from discord.ext import commands
from discord.ext.commands import Bot, Context
from models.command import CommandInfo
from models.constants import Constants
from util.discord.channel import ChannelUtil
from util.discord.messages import Messages
from util.env import Env
from util.regex import RegexUtil, AmountMissingException
from util.number import NumberUtil
from util.validators import Validators
from db.models.stats import Stats
from db.models.transaction import Transaction
from db.models.user import User
from tasks.transaction_queue import TransactionQueue

import asyncio
import config
import cogs.rain as rain
import secrets

## Command documentation
TIP_INFO = CommandInfo(
    triggers = ["ban", "b"] if Env.banano() else ["ntip", "n"],
    overview = "Send a tip to mentioned users",
    details = f"Tip specified amount to mentioned user(s) (**minimum tip is {Constants.TIP_MINIMUM} {Constants.TIP_UNIT}**)" +
        "\nThe recipient(s) will be notified of your tip via private message" +
        "\nSuccessful tips will be deducted from your available balance immediately.\n" +
     f"Example: `{config.Config.instance().command_prefix}{'ban' if Env.banano() else 'ntip'} 2 @user1 @user2` would send 2 to user1 and 2 to user2"
)
TIPSPLIT_INFO = CommandInfo(
    triggers = ["bansplit", "bs"] if Env.banano() else ["ntipsplit", "ns"],
    overview = "Split a tip among mentioned users",
    details = f"Divide the specified amount between mentioned user(s) (**minimum tip is {Constants.TIP_MINIMUM} {Constants.TIP_UNIT}**)" +
        "\nThe recipient(s) will be notified of your tip via private message" +
        "\nSuccessful tips will be deducted from your available balance immediately.\n" +
     f"Example: `{config.Config.instance().command_prefix}{'bansplit' if Env.banano() else 'ntipsplit'} 2 @user1 @user2` would send 1 to user1 and 2 to user2"
)
TIPRANDOM_INFO = CommandInfo(
    triggers = ["banrandom", "br"] if Env.banano() else ["ntiprandom", "ntr"],
    overview = "Tip an active user at random.",
    details = f"Tips the specified amount to an active user at random (**minimum tip is {Constants.TIP_MINIMUM} {Constants.TIP_UNIT}**)" +
        "\nThe recipient will be notified of your tip via private message and you'll be notified of who the random recipient was."
)

class Tips(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def cog_before_invoke(self, ctx: Context):
        ctx.error = False
        # Remove duplicate mentions
        ctx.message.mentions = set(ctx.message.mentions)
        # TODO - incorporate frozen, paused,
        # Only allow tip commands in public channels
        msg = ctx.message
        if ChannelUtil.is_private(msg.channel):
            ctx.error = True
            return
        # See if user exists in DB
        user = await User.get_user(msg.author)
        if user is None:
            ctx.error = True
            await Messages.send_error_dm(msg.author, f"You should create an account with me first, send me `{config.Config.instance().command_prefix}help` to get started.")
            return
        # Update name, if applicable
        await user.update_name(msg.author.name)
        ctx.user = user
        # See if amount meets tip_minimum requirement
        try:
            send_amount = RegexUtil.find_float(msg.content)
            if send_amount < Constants.TIP_MINIMUM:
                raise AmountMissingException(f"Tip amount is too low, minimum is {Constants.TIP_MINIMUM}")
            elif Validators.too_many_decimals(send_amount):
                await Messages.send_error_dm(ctx.message.author, f"You are only allowed to use {Env.precision_digits()} digits after the decimal.")
                ctx.error = True
                return
        except AmountMissingException:
            ctx.error = True
            if ctx.command.name == 'tip_cmd':
                await Messages.send_usage_dm(msg.author, TIP_INFO)
            elif ctx.command.name == 'tipsplit_cmd':
                await Messages.send_usage_dm(msg.author, TIPSPLIT_INFO)
            elif ctx.command.name == 'tiprandom_cmd':
                await Messages.send_usage_dm(msg.author, TIPRANDOM_INFO)
            return
        ctx.send_amount = send_amount

    @commands.command(aliases=TIP_INFO.triggers)
    async def tip_cmd(self, ctx: Context):
        if ctx.error:
            await Messages.add_x_reaction(ctx.message)
            return

        msg = ctx.message
        user = ctx.user
        send_amount = ctx.send_amount

        # Get all eligible users to tip in their message
        users_to_tip = []
        for m in msg.mentions:
            # TODO - consider tip banned
            if not m.bot and m.id != msg.author.id:
                users_to_tip.append(m)
        if len(users_to_tip) < 1:
            await Messages.send_error_dm(msg.author, f"No users you mentioned are eligible to receive tips.")
            return

        # See how much they need to make this tip.
        amount_needed = send_amount * len(users_to_tip)
        available_balance = Env.raw_to_amount(await user.get_available_balance())
        if amount_needed > available_balance:
            await Messages.add_x_reaction(ctx.message)
            await Messages.send_error_dm(msg.author, f"Your balance isn't high enough to complete this tip. You have **{available_balance} {Env.currency_symbol()}**, but this tip would cost you **{amount_needed} {Env.currency_symbol()}**")
            return

        # Make the transactions in the database
        tx_list = []
        for u in users_to_tip:
            tx = await Transaction.create_transaction_internal(
                sending_user=user,
                amount=send_amount,
                receiving_user=u
            )
            tx_list.append(tx)
            asyncio.ensure_future(
                Messages.send_basic_dm(
                    member=u,
                    message=f"You were tipped **{send_amount} {Env.currency_symbol()}** by {msg.author.name.replace('`', '')}.\nUse `{config.Config.instance().command_prefix}mute {msg.author.id}` to disable notifications for this user.",
                    skip_dnd=True
                )
            )
        # Add reactions
        await Messages.add_tip_reaction(msg, send_amount * len(tx_list))
        # Queue the actual sends
        for tx in tx_list:
            await TransactionQueue.instance().put(tx)
        # Update stats
        stats: Stats = await user.get_stats(server_id=msg.guild.id)
        await stats.update_tip_stats(send_amount * len(tx_list))

    @commands.command(aliases=TIPSPLIT_INFO.triggers)
    async def tipsplit_cmd(self, ctx: Context):
        if ctx.error:
            await Messages.add_x_reaction(ctx.message)
            return

        msg = ctx.message
        user = ctx.user
        send_amount = ctx.send_amount

        # Get all eligible users to tip in their message
        users_to_tip = []
        for m in msg.mentions:
            # TODO - consider tip banned
            if not m.bot and m.id != msg.author.id:
                users_to_tip.append(m)
        if len(users_to_tip) < 1:
            await Messages.send_error_dm(msg.author, f"No users you mentioned are eligible to receive tips.")
            return

        individual_send_amount = NumberUtil.truncate_digits(send_amount / len(users_to_tip), max_digits=Env.precision_digits())
        if individual_send_amount < Constants.TIP_MINIMUM:
            await Messages.add_x_reaction(msg)
            await Messages.send_error_dm(msg.author, f"Tip amount too small, each user needs to receive at least {Constants.TIP_MINIMUM}. With your tip they'd only be getting {individual_send_amount}")
            return

        # See how much they need to make this tip.
        amount_needed = individual_send_amount * len(users_to_tip)
        available_balance = Env.raw_to_amount(await user.get_available_balance())
        if amount_needed > available_balance:
            await Messages.send_error_dm(msg.author, f"Your balance isn't high enough to complete this tip. You have **{available_balance} {Env.currency_symbol()}**, but this tip would cost you **{amount_needed} {Env.currency_symbol()}**")
            return

        # Make the transactions in the database
        tx_list = []
        for u in users_to_tip:
            tx = await Transaction.create_transaction_internal(
                sending_user=user,
                amount=individual_send_amount,
                receiving_user=u
            )
            tx_list.append(tx)
            asyncio.ensure_future(
                Messages.send_basic_dm(
                    member=u,
                    message=f"You were tipped **{individual_send_amount} {Env.currency_symbol()}** by {msg.author.name.replace('`', '')}.\nUse `{config.Config.instance().command_prefix}mute {msg.author.id}` to disable notifications for this user.",
                    skip_dnd=True
                )
            )
        # Add reactions
        await Messages.add_tip_reaction(msg, amount_needed)
        # Queue the actual sends
        for tx in tx_list:
            await TransactionQueue.instance().put(tx)
        # Update stats
        stats: Stats = await user.get_stats(server_id=msg.guild.id)
        await stats.update_tip_stats(amount_needed)

    @commands.command(aliases=TIPRANDOM_INFO.triggers)
    async def tiprandom_cmd(self, ctx: Context):
        # TODO - some anti-spam for this command
        if ctx.error:
            await Messages.add_x_reaction(ctx.message)
            return

        msg = ctx.message
        user = ctx.user
        send_amount = ctx.send_amount

        active_users = await rain.Rain.get_active(ctx, excluding=msg.author.id)
        if len(active_users) < Constants.RAIN_MIN_ACTIVE_COUNT:
            await Messages.send_error_dm(msg.author, f"There aren't enough active people to do a random tip. Only **{len(active_users)}** are active, but I'd like to see at least **{Constants.RAIN_MIN_ACTIVE_COUNT}**")
            return

        target_user = secrets.choice(active_users)

        # See how much they need to make this tip.
        available_balance = Env.raw_to_amount(await user.get_available_balance())
        if send_amount > available_balance:
            await Messages.add_x_reaction(ctx.message)
            await Messages.send_error_dm(msg.author, f"Your balance isn't high enough to complete this tip. You have **{available_balance} {Env.currency_symbol()}**, but this tip would cost you **{send_amount} {Env.currency_symbol()}**")
            return

        # Make the transactions in the database
        tx = await Transaction.create_transaction_internal(
            sending_user=user,
            amount=send_amount,
            receiving_user=target_user
        )
        asyncio.ensure_future(
            Messages.send_basic_dm(
                member=target_user,
                message=f"You were randomly selected and received **{send_amount} {Env.currency_symbol()}** from {msg.author.name.replace('`', '')}.\nUse `{config.Config.instance().command_prefix}mute {msg.author.id}` to disable notifications for this user.",
                skip_dnd=True
            )
        )
        asyncio.ensure_future(
            Messages.send_basic_dm(
                member=msg.author,
                message=f'"{target_user.name}" was the recipient of your random tip of {send_amount} {Env.currency_symbol()}'
            )
        )
        # Add reactions
        await Messages.add_tip_reaction(msg, send_amount)
        # Queue the actual send
        await TransactionQueue.instance().put(tx)
        # Update stats
        stats: Stats = await user.get_stats(server_id=msg.guild.id)
        await stats.update_tip_stats(send_amount)
