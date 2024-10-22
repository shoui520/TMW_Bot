from lib.bot import TMWBot
import discord
import re
import aiohttp
import asyncio
import yaml
import os
from datetime import datetime, timedelta
from discord.ext import commands
from discord.utils import utcnow


KOTOBA_BOT_ID = 251239170058616833

ROLE_SETTINGS_PATH = "config/role_settings.yml"

CREATE_QUIZ_ATTEMPTS_TABLE = """
    CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    quiz_name TEXT NOT NULL,
    created_at TIMESTAMP);"""

CREATE_PASSED_QUIZZES_TABLE = """
    CREATE TABLE IF NOT EXISTS passed_quizzes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    quiz_name TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, quiz_name));"""

ADD_QUIZ_ATTEMPT = """INSERT INTO quiz_attempts (guild_id, user_id, quiz_name, created_at) VALUES (?,?,?,?);"""

GET_LAST_QUIZ_ATTEMPT = """SELECT quiz_name, created_at FROM quiz_attempts 
                        WHERE guild_id = ? AND user_id = ? AND quiz_name = ? ORDER BY created_at DESC LIMIT 1;"""

RESET_QUIZ_ATTEMPTS = """DELETE FROM quiz_attempts WHERE guild_id = ? AND user_id = ?"""

ADD_PASSED_QUIZ = """INSERT INTO passed_quizzes (guild_id, user_id, quiz_name) VALUES (?,?,?)
                    ON CONFLICT(guild_id, user_id, quiz_name) DO NOTHING;"""

GET_PASSED_QUIZZES = """SELECT quiz_name FROM passed_quizzes WHERE guild_id = ? AND user_id = ?;"""


async def verify_quiz_settings(quiz_data, quiz_result, member: discord.Member):
    """Ensures a user didn't use cheat settings for the quiz."""
    answer_count = quiz_data["score_limit"]
    answer_time_limit = quiz_data["time_limit"]
    font = quiz_data["font"]
    font_size = quiz_data["font_size"]
    fail_count = quiz_data["max_missed"]

    foreground_color = quiz_data["foreground"]
    effect = quiz_data["effect"]

    if quiz_data["deck_range"]:
        start_index, end_index = quiz_data["deck_range"]
        index_specified = True
    else:
        index_specified = False

    user_count = len(quiz_result["participants"])
    if user_count > 1:
        return False, "Quiz failed due to multiple people participating."

    shuffle = quiz_result["settings"]["shuffle"]
    if not shuffle:
        return False, "Quiz failed due to the shuffle setting being activated."

    is_loaded = quiz_result["isLoaded"]
    if is_loaded:
        return False, "Quiz failed due to being loaded."

    for deck in quiz_result["decks"]:
        if deck["mc"]:
            return False, "Quiz failed due to being set to multiple choice."

    if index_specified:
        for deck in quiz_result["decks"]:
            try:
                if deck["startIndex"] != start_index:
                    return False, "Quiz failed due to having the wrong start index."
                if deck["endIndex"] != end_index:
                    return False, "Quiz failed due to having the wrong end index."
            except KeyError:
                return False, "Quiz failed due to not having an index specified."
    else:
        for deck in quiz_result["decks"]:
            try:
                if deck["startIndex"]:
                    return False, "Quiz failed due to having a start index."
            except KeyError:
                pass
            try:
                if deck["endIndex"]:
                    return False, "Quiz failed due to having an end index."
            except KeyError:
                pass

    if foreground_color:
        if quiz_result["settings"]["fontColor"] != foreground_color:
            return False, "Foreground color does not match required color."

    if effect:
        if quiz_result["settings"]["effect"] != effect:
            return False, "Effect does not match required effect."

    if answer_count != quiz_result["settings"]["scoreLimit"]:
        return False, "Set score limit and required score limit don't match."

    if answer_time_limit != quiz_result["settings"]["answerTimeLimitInMs"]:
        return False, "Set answer time does match required answer time."

    if font and font != quiz_result["settings"]["font"]:
        return False, "Set font does not match required font."

    if font_size and font_size != quiz_result["settings"]["fontSize"]:
        return False, "Set font size does not match required font size."

    failed_question_count = len(quiz_result["questions"]) - quiz_result["scores"][0]["score"]
    if failed_question_count > fail_count:
        return False, "Failed too many questions."

    if answer_count != quiz_result["scores"][0]["score"]:
        return False, "Not enough questions answered."

    return (
        True,
        f"{member.mention} has passed the {quiz_data['name']} quiz!")


async def get_quiz_id(message: discord.Message):
    """Extract the ID of a quiz to use with the API."""
    try:
        if "Ended" in message.embeds[0].title:
            return re.findall(r"game_reports/([\da-z]*)", message.embeds[0].fields[-1].value)[0]
    except IndexError:
        return False
    except TypeError:
        return False


kotoba_request_lock = asyncio.Lock()


async def extract_quiz_result_from_id(quiz_id):
    async with kotoba_request_lock:
        await asyncio.sleep(2)
        jsonurl = f"https://kotobaweb.com/api/game_reports/{quiz_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(jsonurl) as resp:
                return await resp.json()


async def timeout_member(member: discord.Member, duration_in_minutes: int, reason: str):
    try:
        await member.timeout(utcnow() + timedelta(minutes=duration_in_minutes), reason=reason)
    except discord.Forbidden:
        pass


class LevelUp(commands.Cog):
    def __init__(self, bot: TMWBot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.RUN(CREATE_QUIZ_ATTEMPTS_TABLE)
        await self.bot.RUN(CREATE_PASSED_QUIZZES_TABLE)
        with open(ROLE_SETTINGS_PATH, "r") as f:
            self.role_settings = yaml.safe_load(f)

    async def is_in_levelup_channel(self, message: discord.Message):
        channel_ids = self.role_settings['settings'][message.guild.id]['valid_levelup_channels']
        channels = [message.guild.get_channel(channel_id) for channel_id in channel_ids]
        return message.channel in channels

    async def is_restricted_quiz(self, message: discord.Message):
        restricted_quizzes = self.role_settings['settings'][message.guild.id]['restricted_quiz_names']
        for quiz_name in restricted_quizzes:
            if quiz_name.lower() in message.content.lower():
                return quiz_name, True
        return None, False

    async def is_valid_quiz(self, message: discord.Message, rank_structure: dict):
        for quiz in rank_structure:
            if message.content == quiz['command']:
                return True, quiz['name']
        return False, None

    async def is_command_input_valid(self, message: discord.Message):
        if message.author.bot:
            return True

        restricted_quiz_name, is_restricted = await self.is_restricted_quiz(message)
        is_in_levelup_channel = await self.is_in_levelup_channel(message)
        is_valid_quiz, performed_quiz_name = await self.is_valid_quiz(message, self.role_settings['rank_structure'][message.guild.id])

        is_on_cooldown = await self.is_on_cooldown(message, performed_quiz_name)
        if is_on_cooldown:
            await timeout_member(message.author, 2, "Quiz on cooldown.")
            return False

        if is_in_levelup_channel and not is_valid_quiz:
            await message.channel.send(f"{message.author.mention} Please use the exact quiz command in the level-up channel.")
            await timeout_member(message.author, 2, "Invalid quiz attempt.")
            return False

        if is_restricted:
            if not is_in_levelup_channel or not is_valid_quiz:
                await message.channel.send(f"{message.author.mention} {restricted_quiz_name} quiz is restricted.\nYou can only use it in the level-up channel with the exact commands.")
                await timeout_member(message.author, 2, "Restricted quiz attempt.")
                return False

        if is_valid_quiz and not is_in_levelup_channel:
            await message.channel.send(f"{message.author.mention} Please use this quiz command in the level-up channels.")
            await timeout_member(message.author, 2, "Invalid channel for quiz attempt.")
            return False

        if is_valid_quiz and is_in_levelup_channel:
            await self.register_quiz_attempt(message, performed_quiz_name)

        return True

    async def is_on_cooldown(self, message: discord.Message, quiz_name):
        last_attempt = await self.bot.GET_ONE(GET_LAST_QUIZ_ATTEMPT, (message.guild.id, message.author.id, quiz_name))
        if not last_attempt:
            return False
        quiz_name, last_attempt_time = last_attempt
        last_attempt_time = datetime.fromisoformat(last_attempt_time)
        if last_attempt_time + timedelta(days=7) > utcnow():
            next_attempt_time = last_attempt_time + timedelta(days=7)
            unix_timestamp = int(next_attempt_time.timestamp())

            await message.channel.send(
                f"{message.author.mention} You can only attempt this quiz once every 7 days. Your next attempt will be available <t:{unix_timestamp}:R> (on <t:{unix_timestamp}:F>).")
            return True

    async def register_quiz_attempt(self, message: discord.Message, quiz_name):
        await self.bot.RUN(ADD_QUIZ_ATTEMPT, (message.guild.id, message.author.id, quiz_name, utcnow()))
        await message.channel.send(f"{message.author.mention} registering attempt for {quiz_name}. You can try again in 7 days.")

    async def get_corresponding_quiz_data(self, message: discord.Message, quiz_result: dict):
        rank_structure = self.role_settings['rank_structure'][message.guild.id]
        deck_names = [deck['shortName'] for deck in quiz_result["decks"]]
        for rank in rank_structure:
            if set(rank['decks']) == set(deck_names):
                return rank

    async def get_all_quiz_roles(self, guild: discord.Guild):
        rank_structure = self.role_settings['rank_structure'][guild.id]
        return [guild.get_role(role['rank_to_get']) for role in rank_structure if role['rank_to_get']]

    async def reward_user(self, member: discord.Member, quiz_data: dict):
        await self.bot.RUN(ADD_PASSED_QUIZ, (member.guild.id, member.id, quiz_data['name']))
        if quiz_data['rank_to_get']:
            roles = await self.get_all_quiz_roles(member.guild)
            role_to_get = member.guild.get_role(quiz_data['rank_to_get'])
            await member.remove_roles(*roles)
            await member.add_roles(role_to_get)
        else:
            await self.check_if_combination_rank_earned(member)

    async def check_if_combination_rank_earned(self, member: discord.Member):
        rank_structure = self.role_settings['rank_structure'][member.guild.id]
        combination_ranks = [rank_data for rank_data in rank_structure if rank_data['combination_rank'] is True]
        earned_ranks = await self.bot.GET(GET_PASSED_QUIZZES, (member.guild.id, member.id))
        earned_ranks = [rank[0] for rank in earned_ranks]
        # Reverse for correct hierarchy.
        combination_ranks.reverse()
        for rank in combination_ranks:

            combination_role = member.guild.get_role(rank['rank_to_get'])
            if combination_role in member.roles:
                return

            if all(quiz_name in earned_ranks for quiz_name in rank['quizzes_required']):
                roles = await self.get_all_quiz_roles(member.guild)
                await member.remove_roles(*roles)
                role_to_get = member.guild.get_role(rank['rank_to_get'])
                all_rank_roles = await self.get_all_quiz_roles(member.guild)
                assigned_rank_roles = [role for role in member.roles if role in all_rank_roles]
                if all_rank_roles.index(role_to_get) <= all_rank_roles.index(assigned_rank_roles[0]):
                    return
                await member.add_roles(role_to_get)
                announcement_channel = member.guild.get_channel(
                    self.role_settings['settings'][member.guild.id]['announce_channel'])
                await announcement_channel.send(f"{member.mention} is now a {role_to_get.name}!")
                return

    @commands.Cog.listener(name="on_message")
    async def level_up_routine(self, message: discord.Message):
        if not message.author.id == KOTOBA_BOT_ID and not 'k!q' in message.content.lower():
            return

        is_valid_command = await self.is_command_input_valid(message)
        if not is_valid_command:
            return

        quiz_id = await get_quiz_id(message)
        if not quiz_id:
            return

        quiz_result = await extract_quiz_result_from_id(quiz_id)
        quiz_data = await self.get_corresponding_quiz_data(message, quiz_result)
        member = message.guild.get_member(int(quiz_result["participants"][0]["discordUser"]["id"]))

        success, quiz_message = await verify_quiz_settings(quiz_data, quiz_result, member)

        role_to_get = member.guild.get_role(quiz_data['rank_to_get'])
        if role_to_get:
            all_rank_roles = await self.get_all_quiz_roles(member.guild)
            assigned_rank_roles = [role for role in member.roles if role in all_rank_roles]
            if all_rank_roles.index(role_to_get) <= all_rank_roles.index(assigned_rank_roles[0]):
                return

        if success:
            announcement_channel = message.guild.get_channel(
                self.role_settings['settings'][message.guild.id]['announce_channel'])
            await announcement_channel.send(quiz_message)
            await self.reward_user(member, quiz_data)
        else:
            await message.channel.send(quiz_message)
            return

    @discord.app_commands.command(name="reset_user_cooldown",  description="Reset a users quiz cooldown.")
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(user="The user to clear the cooldown of.")
    @discord.app_commands.default_permissions(administrator=True)
    async def clear_user_cooldown(self, interaction: discord.Interaction, user: discord.Member):
        await self.bot.RUN(RESET_QUIZ_ATTEMPTS, (interaction.guild.id, user.id))
        await interaction.response.send_message(f"Cleared quiz cooldown for {user.mention}.")

    @discord.app_commands.command(name="ranktable",  description="Display the distribution of quiz roles in the server.")
    @discord.app_commands.guild_only()
    async def ranktable(self, interaction: discord.Interaction):
        quiz_roles = await self.get_all_quiz_roles(interaction.guild)
        description = "\n".join([f"{role.mention}: {len(role.members)} ({len(
            role.members) / interaction.guild.member_count * 100:.2f}%)" for role in quiz_roles])

        description += f"\n\nTotal member count: {interaction.guild.member_count}"

        embed = discord.Embed(title=f"Role Distribution", description=description, color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @discord.app_commands.command(name="rankusers", description="See all users with a specific role.")
    @discord.app_commands.describe(role="Role for which all members should be displayed.")
    @discord.app_commands.guild_only()
    async def rankusers(self, interaction: discord.Interaction, role: discord.Role):
        member_count = len(role.members)
        mention_string = []
        for member in role.members:
            mention_string.append(member.mention)
        if len(" ".join(mention_string)) < 500:
            mention_string.append(f"\n\nA total {member_count} members have the role {role.mention}.")
            await interaction.response.send_message(" ".join(mention_string), allowed_mentions=discord.AllowedMentions.none(), ephemeral=True)
        else:
            member_string = [str(member) for member in role.members]
            member_string.append(f"\nTotal {member_count} members.")
            with open("data/rank_user_count.txt", "w") as text_file:
                text_file.write("\n".join(member_string))
            await interaction.response.send_message("List of role members too large. Providing role member list in a file:", file=discord.File("data/rank_user_count.txt"), ephemeral=True)
            os.remove("data/rank_user_count.txt")


async def setup(bot):
    await bot.add_cog(LevelUp(bot))