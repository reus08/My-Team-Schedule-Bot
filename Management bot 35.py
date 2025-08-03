# ======================
# SECTION 1: IMPORTS AND CONSTANTS
# ======================

from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.ext import CallbackContext
from functools import wraps
import httpx
import asyncio
import datetime
import re
import gspread
import pytz
from gspread.exceptions import APIError, GSpreadException
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    ConversationHandler,
    filters, JobQueue
)
import random
import string
from datetime import timedelta
from dotenv import load_dotenv
import os

# Google Sheets Configuration
PENDING_PROJECTS_SHEET = 'Pending Projects'
PENDING_JOINS_SHEET = 'Pending_Joins'


load_dotenv()  # Load variables from .env
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")               # ✅ Safe!
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")                    # ✅ Safe!
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_CREDENTIALS_PATH") # ✅ Safe!

ADDED_REMINDERS_SHEET = 'Added Reminders'
REQUEST_DELETE_SHEET = 'Request Delete'
LOG_SHEET = 'Users Log'
TIMEZONE_SHEET = 'Timezone'
PROJ_MANAGERS_SHEET = 'Proj Managers'
MEMBERS_SHEET = 'Members'
REMINDERS_ROOT_SHEET = 'RemindersRoot'
ADMIN_SHEET = 'Admin List'

# Timezone for Philippines
PH_TZ = pytz.timezone('Asia/Manila')

# Track pending notifications
pending_notifications = set()

# In-memory cache for faster access {chat_id: last_id}
id_cache = {
    'reminders': {}  # For reminder IDs only
}

# Conversation states
ADD_ADMIN_SPACE_SELECT, ADD_ADMIN_INPUT, ADD_ADMIN_CONFIRM = range(50, 53)
DEL_ADMIN_SELECT, DEL_ADMIN_CONFIRM = range(53, 55)
DEL_PROJECT_SELECT, DEL_PROJECT_CONFIRM = range(33, 35)
ASSIGN_REM_SELECT_MEMBER, ASSIGN_REM_INPUT, ASSIGN_REM_PROJECT_SELECT, ASSIGN_REM_CONFIRM = range(40, 44)
SUGGEST_SPACE_SELECT, SUGGEST_PROJECT_NAME = range(35, 37)
DEL_PROJECT_SELECT, DEL_PROJECT_CONFIRM = range(33, 35)
ADD_REMINDER_PROJECT_SELECT = 13
ADD_PROJECT_NAME, ADD_PROJECT_SPACE, ADD_PROJECT_CONFIRM = range(30, 33)
UNJOIN_SPACE_SELECT, UNJOIN_SPACE_CONFIRM = range(28, 30)
REGISTER_MANAGER_NAME_INPUT = 25  # Used for /addspace flow
REVOKE_CODE_SELECT, REVOKE_CODE_CONFIRM = range(26, 28)
ADD_REMINDER_INPUT, ADD_REMINDER_CONFIRM = range(10, 12)
DEL_REMINDER_INPUT = range(12, 13)
TIMEZONE_INPUT, TIMEZONE_CONFIRM = range(6, 8)
REGISTER_CHOOSE, REGISTER_MANAGER_INPUT, REGISTER_MANAGER_CONFIRM, REGISTER_MEMBER_INPUT, REGISTER_MEMBER_CONFIRM = range(
    20, 25)

# Recurrence types
RECURRENCE_TYPES = {
    'once': {'pattern': 'Once', 'input_prompt': "Enter date, time, reminder (e.g. 6/21/25, 8:00 PM, Pulong ng Buklod)"},
    'daily': {'pattern': 'Daily', 'input_prompt': "Enter time, reminder (e.g. 8:00 AM, Morning devotion)"},
    'weekly': {'pattern': 'Weekly', 'input_prompt': "Enter day, time, reminder (e.g. Mon, 7:00 PM, Family night)"},
    'monthly': {'pattern': 'Monthly', 'input_prompt': "Enter day number, time, reminder (e.g. 15, 5:00 PM, Pay bills)"},
    'yearly': {'pattern': 'Yearly',
               'input_prompt': "Enter month, day, time, reminder (e.g. Dec, 25, 8:00 AM, Christmas)"}
}


# ======================
# SECTION 2: HELPER FUNCTIONS
# ======================
def is_member_of_space(chat_id: str, space_code: str) -> bool:
    """Check if user is a member of the specified space"""
    try:
        worksheet = init_google_sheets(MEMBERS_SHEET)
        all_values = worksheet.get_all_values()

        for row in all_values[1:]:  # Skip header
            if len(row) >= 4 and row[0] == chat_id and row[3] == space_code:
                return True
        return False
    except Exception as e:
        print(f"Error checking member: {e}")
        return False


def get_member_name(chat_id: str) -> str:
    """Get member name from chat ID"""
    try:
        worksheet = init_google_sheets(MEMBERS_SHEET)
        cell = worksheet.find(chat_id)
        if cell:
            return worksheet.cell(cell.row, 3).value  # Column C is member name
        return "Unknown Member"
    except Exception as e:
        print(f"Error getting member name: {e}")
        return "Unknown Member"


def count_user_admins(chat_id: str, space_code: str) -> int:
    """Count how many admins a user has added to a specific space"""
    try:
        worksheet = init_google_sheets(ADMIN_SHEET)
        all_values = worksheet.get_all_values()

        count = 0
        for row in all_values[1:]:  # Skip header
            if len(row) >= 6 and row[0] == chat_id and row[3] == space_code:
                count += 1
        return count
    except Exception as e:
        print(f"Error counting admins: {e}")
        return 0


def get_user_admins(chat_id: str) -> list:
    """Get all admins added by a user"""
    try:
        worksheet = init_google_sheets(ADMIN_SHEET)
        all_values = worksheet.get_all_values()

        admins = []
        for row in all_values[1:]:  # Skip header
            if len(row) >= 6 and row[0] == chat_id:
                admins.append({
                    'space_code': row[3],
                    'space_name': get_space_name(row[3]),
                    'admin_chat_id': row[4],
                    'admin_name': row[5]
                })
        return admins
    except Exception as e:
        print(f"Error getting admins: {e}")
        return []


def get_space_name(space_code: str) -> str:
    """Get space name from code"""
    try:
        worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        cell = worksheet.find(space_code)
        if cell:
            return worksheet.cell(cell.row, 5).value  # Column E is space name
        return "Unnamed Space"
    except Exception as e:
        print(f"Error getting space name: {e}")
        return "Unnamed Space"


def init_pending_joins_sheet():
    """Initialize the Pending Joins sheet with headers if needed"""
    try:
        worksheet = init_google_sheets(PENDING_JOINS_SHEET)
        # Check if headers exist
        if not worksheet.get_values('A1:G1'):
            worksheet.update('A1:G1', [
                ['ManagerChatID', 'Timestamp', 'MemberChatID', 'MemberName',
                 'Code', 'SpaceName', 'Status']
            ])
        return worksheet
    except Exception as e:
        print(f"Error initializing Pending Joins sheet: {e}")
        raise


def generate_space_code():
    """Generate a random 4-character alphanumeric code (all caps)"""
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choices(characters, k=4))


async def validate_code_id(code_id: str) -> bool:
    """Validate that the code is exactly 4 characters long and contains only uppercase letters and numbers"""
    if len(code_id) != 4:
        return False

    # Check each character is either A-Z or 0-9
    for char in code_id:
        if not (char.isupper() and char.isalpha()) and not char.isdigit():
            return False

    return True


async def show_loading_indicator(update: Update, context: CallbackContext, message_text="⏳ On it...") -> Message:
    """Show a loading indicator message that can be deleted later"""
    try:
        loading_message = await update.message.reply_text(
            message_text,
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['loading_message_id'] = loading_message.message_id
        return loading_message
    except Exception as e:
        print(f"Error showing loading indicator: {e}")
        return None


async def delete_loading_indicator(update: Update, context: CallbackContext):
    """Delete the loading indicator if it exists"""
    try:
        if 'loading_message_id' in context.user_data:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['loading_message_id']
            )
            del context.user_data['loading_message_id']
    except Exception as e:
        print(f"Error deleting loading indicator: {e}")


async def cleanup_messages(update: Update, context: CallbackContext, num_messages: int = 2):
    """Deletes the last 'num_messages' sent by the bot and the user's last message."""
    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    # Delete the user's last message
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Error deleting user's message: {e}")

    # Delete previous bot messages (including loading indicator if it wasn't deleted)
    for i in range(1, num_messages + 2):  # +2 to account for loading and potential previous bot message
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id - i)
        except Exception:
            # Ignore if message doesn't exist (e.g., already deleted or not a bot message)
            pass

    # Ensure loading indicator is deleted if it was set
    if 'loading_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=context.user_data['loading_message_id']
            )
            del context.user_data['loading_message_id']
        except Exception as e:
            print(f"Error deleting loading indicator during cleanup: {e}")


def init_google_sheets(sheet_name):
    try:
        scope = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_JSON, scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        return spreadsheet.worksheet(sheet_name)
    except (APIError, GSpreadException) as e:
        print(f"Error initializing Google Sheets: {str(e)}")
        raise


def init_reminder_id_tracker():
    """Initialize the reminder ID tracker sheet with headers if needed"""
    try:
        worksheet = init_google_sheets("Reminder_ID_Tracker")
        # Check if headers exist
        if not worksheet.get_values('A1:B1'):
            worksheet.update('A1:B1', [['chat_id', 'last_id']])
        return worksheet
    except Exception as e:
        print(f"Error initializing schedules ID tracker: {e}")
        raise


async def get_next_reminder_id(chat_id: int) -> int:
    """Get next reminder ID (1-500) with RAM cache + Google Sheets backup"""
    try:
        # Check RAM cache first
        if chat_id in id_cache['reminders']:
            new_id = (id_cache['reminders'][chat_id] % 500) + 1
            id_cache['reminders'][chat_id] = new_id
        else:
            # Fallback to Google Sheets
            worksheet = init_reminder_id_tracker()
            cell = worksheet.find(str(chat_id))

            if cell:  # Existing user
                current_id = int(worksheet.cell(cell.row, 2).value)
                new_id = (current_id % 500) + 1
                worksheet.update_cell(cell.row, 2, new_id)
            else:  # New user
                new_id = 1
                worksheet.append_row([chat_id, new_id])

            id_cache['reminders'][chat_id] = new_id

        return new_id
    except Exception as e:
        print(f"Reminder ID System Error: {e}")
        return 1  # Fallback if both systems fail


def parse_flexible_date(date_str: str) -> tuple:
    """Parse various date formats into (month, day, year)"""
    try:
        # Try MM/DD/YY or MM/DD/YYYY
        if '/' in date_str:
            parts = date_str.split('/')
            if len(parts) == 3:
                month = int(parts[0])
                day = int(parts[1])
                year = int(parts[2])
                if year < 100:  # 2-digit year
                    year += 2000
                return month, day, year

        # Try MMDDYY or MMDDYYYY
        if date_str.isdigit():
            if len(date_str) == 6:  # MMDDYY
                month = int(date_str[:2])
                day = int(date_str[2:4])
                year = 2000 + int(date_str[4:6])
                return month, day, year
            elif len(date_str) == 8:  # MMDDYYYY
                month = int(date_str[:2])
                day = int(date_str[2:4])
                year = int(date_str[4:8])
                return month, day, year

        # Try month name formats (June 21 2025, Jun 21 25, etc.)
        month_names = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
        }

        date_str_lower = date_str.lower()
        for month_name, month_num in month_names.items():
            if month_name in date_str_lower:
                # Extract day and year
                parts = re.split(r'[\s,]+', date_str)
                day = int(re.search(r'\d+', parts[1]).group())
                year_match = re.search(r'\d+', parts[2] if len(parts) > 2 else '')
                year = int(year_match.group()) if year_match else datetime.datetime.now().year
                if year < 100:
                    year += 2000
                return month_num, day, year

    except (ValueError, IndexError, AttributeError):
        pass

    raise ValueError("Invalid date format")


def parse_flexible_time(time_str: str) -> str:
    """Parse various time formats into HH:MM AM/PM"""
    time_str = time_str.strip().upper()

    # Handle cases like "9am", "10:00am", "10:00 pm", "1 PM"
    time_str = re.sub(r'([AP])(M)', r'\1\2', time_str)  # Fix AM/PM spacing
    time_str = re.sub(r'(\d)([AP]M)', r'\1 \2', time_str)  # Add space if missing

    # Extract time parts
    time_match = re.match(r'(\d{1,2})(?::(\d{2}))?\s*([AP]M)?', time_str)
    if not time_match:
        raise ValueError("Invalid time format")

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    period = time_match.group(3) or ('AM' if hour < 12 else 'PM')

    # Convert to 12-hour format
    if hour > 12:
        hour -= 12
        period = 'PM'
    elif hour == 0:
        hour = 12
        period = 'AM'

    return f"{hour}:{minute:02d} {period}"


def parse_flexible_recurrence(recurrence_str: str) -> str:
    """Normalize recurrence input"""
    recurrence_str = recurrence_str.strip().lower()

    mapping = {
        'o': 'Once',
        'once': 'Once',
        'd': 'Daily',
        'daily': 'Daily',
        'w': 'Weekly',
        'week': 'Weekly',
        'weekly': 'Weekly',
        'm': 'Monthly',
        'month': 'Monthly',
        'monthly': 'Monthly',
        'y': 'Yearly',
        'year': 'Yearly'
    }

    # Find the best match
    for key, value in mapping.items():
        if recurrence_str.startswith(key):
            return value

    raise ValueError("Invalid recurrence. Use: Once/Daily/Weekly/Monthly/Yearly")


async def handle_invalid_input(update: Update, context: CallbackContext) -> int:
    """Handle any invalid input during conversations"""
    if update.message:
        await update.message.reply_text(
            "❌ Please use /submit to confirm or /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )
    # Return the current state to maintain conversation
    return getattr(context, 'user_data', {}).get('current_state', ConversationHandler.END)


async def send_message_safe(chat_id, text, context, parse_mode=ParseMode.MARKDOWN):
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode
        )
    except Exception:
        # If Markdown fails, try sending as plain text
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text
            )
        except Exception as fallback_error:
            print(f"Failed to send message (both markdown and plain): {fallback_error}")


async def log_registration(update: Update):
    try:
        worksheet = init_google_sheets(LOG_SHEET)
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
        location = "Unknown"
        if update.message and update.message.location:
            location = f"{update.message.location.latitude}, {update.message.location.longitude}"

        row_data = [
            update.message.chat_id,
            timestamp,
            update.message.from_user.full_name,
            location
        ]
        worksheet.append_row(row_data)
    except Exception as e:
        print(f"Error logging registration: {str(e)}")


async def space_command(update: Update, context: CallbackContext) -> None:
    """Show all space-related commands"""
    await update.message.reply_text(
        "🚀 *Space Management Commands*\n\n"
        "/addspace - Create a new TeamSpace\n"
        "/deletespace - Delete a TeamSpace\n"
        "/showspace - List your TeamSpaces\n"
        "/joinspace - Join an existing TeamSpace\n"
        "/unjoinspace - Leave a TeamSpace\n\n"
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )
    # await cleanup_messages(update, context, num_messages=1)


async def project_command(update: Update, context: CallbackContext) -> None:
    """Show all project-related commands"""
    await update.message.reply_text(
        "📂 *Project Management Commands*\n\n"
        "/addproject - Create a new project/sections.\n"
        "/deleteproject - Delete a project.\n"
        "/showproject - List your projects.\n\n"
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )
    # await cleanup_messages(update, context, num_messages=0)


async def schedule_command(update: Update, context: CallbackContext) -> None:
    """Show all schedule-related commands"""
    await update.message.reply_text(
        "⏰ *Schedule Management Commands*\n\n"
        "/addsched - Add a new schedule.\n"
        "/deletesched - Delete a schedule.\n"
        "/showsched - List your schedules.\n"
        "/assignsched - Assign schedule to team member.\n\n"  # Changed from /assignrem
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )
    # await cleanup_messages(update, context, num_messages=0)


async def status_command(update: Update, context: CallbackContext) -> None:
    """Show all status-related commands"""
    await update.message.reply_text(
        "📊 *Status Check Commands*\n\n"
        "/joinstatus - Check join request status\n"
        "/suggestprojectstatus - Check project suggestion status\n\n"
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )
    # await cleanup_messages(update, context, num_messages=0)


async def settings_command(update: Update, context: CallbackContext) -> None:
    """Show all settings-related commands"""
    await update.message.reply_text(
        "⚙️ *Settings Commands*\n\n"
        "/timezone - Set your timezone (if outside PH)\n"
        "/chatid - Get your chat ID\n"
        "/guidelines - Usage guidelines\n"
        "/help - Show all commands\n"
        "/cancel - Cancel current operation\n\n"
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )
    # await cleanup_messages(update, context, num_messages=0)


async def monitoring_command(update: Update, context: CallbackContext) -> None:
    """Show all monitoring-related commands"""
    await update.message.reply_text(
        "👁️ *Monitoring Commands*\n\n"
        "/schedtoday - Today's schedule\n"
        "/schedtomorrow - Tomorrow's schedule\n"
        "/schedthisweek - This week's schedule\n"
        "/members - List team members\n\n"
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )
    # await cleanup_messages(update, context, num_messages=0)


async def member_command(update: Update, context: CallbackContext) -> None:
    """Show all member-related commands"""
    await update.message.reply_text(
        "👥 *Member Management Commands*\n\n"
        "/showmember - List all members in your spaces\n"
        "/deletemember - Remove a member from your space\n\n"
        "ℹ️ Use /help for more details about each command",
        parse_mode=ParseMode.MARKDOWN
    )


# ======================
# SECTION 3: COMMAND HANDLERS
# ======================
async def admin_command(update: Update, context: CallbackContext) -> None:
    """Show all admin management commands in one place"""
    await update.message.reply_text(
        "👨‍💼 *Admin Management Commands*\n\n"
        "/addadmin - Add a new admin to your space\n"
        "/deleteadmin - Remove an admin from your space\n"
        "/showadmin - List all admins in your spaces\n\n"
        "ℹ️ *Usage Notes:*\n"
        "- Only space managers can use these commands\n"
        "- Max 4 admins per space\n"
        "- Admins can view all members' schedules\n\n"
        "🔍 Need help? Use /help",
        parse_mode=ParseMode.MARKDOWN
    )


async def addadmin_command(update: Update, context: CallbackContext) -> int:
    """Start the admin addition process"""
    try:
        context.user_data.clear()
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your spaces...")

        # Check if user is a manager
        worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        chat_id = str(update.message.chat_id)
        all_values = worksheet.get_all_values()

        # Get all spaces this manager owns
        manager_spaces = {}
        for row in all_values[1:]:  # Skip header
            if row[0] == chat_id and len(row) >= 5:  # Check chat_id in column A
                space_code = row[3]  # Column D
                space_name = row[4] if len(row) > 4 else 'Unnamed Space'  # Column E
                manager_spaces[space_code] = space_name

        await delete_loading_indicator(update, context)

        if not manager_spaces:
            await update.message.reply_text(
                "ℹ️ You haven't created any spaces yet. Create one first with /addspace",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the spaces list with / commands
        spaces_list = "\n".join(
            f"/{code} - {name}"  # Add / prefix to code
            for code, name in manager_spaces.items()
        )

        # Store spaces data
        context.user_data['admin_spaces'] = manager_spaces

        await update.message.reply_text(
            "👥 *Add Admin to Space*\n\n"
            "Which space would you like to add an admin to?\n\n"
            f"{spaces_list}\n\n"
            "Tap on the space code above to select it (it will auto-send)\n\n"
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return ADD_ADMIN_SPACE_SELECT

    except Exception as e:
        print(f"Error in addadmin_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your spaces. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_admin_space_select(update: Update, context: CallbackContext) -> int:
    """Handle selected space for admin addition"""
    try:
        selected_code_with_slash = update.message.text.strip()
        # Remove leading '/' if present
        selected_code = selected_code_with_slash.lstrip('/').upper()
        spaces = context.user_data.get('admin_spaces', {})

        if selected_code not in spaces:
            await update.message.reply_text(
                "❌ Invalid code selection. Please choose from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_ADMIN_SPACE_SELECT

        # Check admin limit (max 4 per space)
        admin_count = count_user_admins(str(update.message.chat_id), selected_code)
        if admin_count >= 4:
            await update.message.reply_text(
                "❌ *Admin limit reached!*\n\n"
                "You can only add up to 4 admins per space.\n\n"
                "Use /deleteadmin to remove existing admins first.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Store selected space
        context.user_data['admin_space_code'] = selected_code
        context.user_data['admin_space_name'] = spaces[selected_code]

        await update.message.reply_text(
            "👤 *Enter Admin Details*\n\n"
            "Please send the *Chat ID* of the person you want to make admin:\n\n"
            "Example: `123456789`\n\n"
            "They will be able to view all members' schedules in this space.",
            parse_mode=ParseMode.MARKDOWN
        )

        return ADD_ADMIN_INPUT

    except Exception as e:
        print(f"Error in handle_admin_space_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_ADMIN_SPACE_SELECT


async def handle_admin_input(update: Update, context: CallbackContext) -> int:
    """Process admin chat ID input with validation"""
    try:
        admin_chat_id = update.message.text.strip()
        if not admin_chat_id.isdigit():
            await update.message.reply_text(
                "❌ Invalid Chat ID! Please enter numbers only.\n\n"
                "Example: `123456789`\n\n"
                "Try again or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_ADMIN_INPUT

        space_code = context.user_data.get('admin_space_code')
        if not space_code:
            await update.message.reply_text(
                "❌ Space selection lost. Start over with /addadmin",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "🔍 Verifying member...")

        # Check if user is member of this space
        if not is_member_of_space(admin_chat_id, space_code):
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                f"❌ User {admin_chat_id} is not a member of this space!\n\n"
                "They must join the space first before becoming admin.\n"
                "Ask them to use /joinspace with your space code.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_ADMIN_INPUT

        # Get member name
        admin_name = get_member_name(admin_chat_id)

        # Store admin details
        context.user_data['admin_chat_id'] = admin_chat_id
        context.user_data['admin_name'] = admin_name

        await delete_loading_indicator(update, context)

        # Show confirmation
        await update.message.reply_text(
            f"👤 *Confirm Admin Addition*\n\n"
            f"*Space:* {context.user_data.get('admin_space_name', '')}\n"
            f"*Admin Name:* {admin_name}\n"
            f"*Chat ID:* {admin_chat_id}\n\n"
            "*Permissions:*\n"
            "- View all members' schedules\n"
            "- Assign schedules to members\n\n"
            "Is this correct?\n\n"
            "✅ Yes - /submit\n"
            "❌ No - /cancel",
            parse_mode=ParseMode.MARKDOWN
        )

        return ADD_ADMIN_CONFIRM

    except Exception as e:
        print(f"Error in handle_admin_input: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "❌ Error processing admin details. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_ADMIN_INPUT


async def submit_addadmin(update: Update, context: CallbackContext) -> int:
    """Save the admin to Google Sheets after final validation"""
    try:
        admin_data = context.user_data
        required_keys = ['admin_space_code', 'admin_chat_id', 'admin_name']
        if not all(key in admin_data for key in required_keys):
            await update.message.reply_text(
                "❌ Missing admin data. Start over with /addadmin",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Final validation - check admin limit
        admin_count = count_user_admins(str(update.message.chat_id), admin_data['admin_space_code'])
        if admin_count >= 4:
            await update.message.reply_text(
                "❌ *Admin limit reached!*\n\n"
                "You can only have 4 admins per space.\n"
                "Remove some admins first with /deleteadmin",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Adding admin...")

        # Save to Admin List sheet
        worksheet = init_google_sheets(ADMIN_SHEET)
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
        row_data = [
            update.message.chat_id,
            timestamp,
            update.message.from_user.full_name,
            admin_data['admin_space_code'],
            admin_data['admin_chat_id'],
            admin_data['admin_name']
        ]
        worksheet.append_row(row_data)

        await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"✅ *Admin added successfully!*\n\n"
            f"*Space:* {admin_data.get('admin_space_name', '')}\n"
            f"*Admin Name:* {admin_data['admin_name']}\n"
            f"*Chat ID:* {admin_data['admin_chat_id']}\n\n"
            "They can now view all members' schedules in this space.",
            parse_mode=ParseMode.MARKDOWN
        )
        await cleanup_messages(update, context, num_messages=3)

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_addadmin: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to add admin: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def deleteadmin_command(update: Update, context: CallbackContext) -> int:
    """Start the admin deletion process"""
    try:
        context.user_data.clear()
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your admins...")

        chat_id = str(update.message.chat_id)
        admins = get_user_admins(chat_id)

        await delete_loading_indicator(update, context)

        if not admins:
            await update.message.reply_text(
                "ℹ️ You haven't added any admins yet.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the admins list with / commands
        admins_list = []
        for i, admin in enumerate(admins, start=1):
            admins_list.append(
                f"/{i} - {admin['admin_name']} ({admin['admin_chat_id']}) - {admin['space_name']}"
            )

        # Store admins for selection
        context.user_data['delete_admins'] = admins

        await update.message.reply_text(
            "🗑 *Remove Admin*\n\n"
            "Which admin would you like to remove?\n\n"
            + "\n".join(admins_list) + "\n\n"
                                       "Tap on the number above to select it\n\n"
                                       "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_ADMIN_SELECT

    except Exception as e:
        print(f"Error in deleteadmin_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your admins. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_deleteadmin_select(update: Update, context: CallbackContext) -> int:
    """Handle selected admin for deletion"""
    try:
        command = update.message.text.strip()
        if not command.startswith('/'):
            await update.message.reply_text(
                "❌ Please select an admin using the numbered commands.",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_ADMIN_SELECT

        # Extract number from command
        selected_num = command[1:]  # Remove the leading '/'
        admins = context.user_data.get('delete_admins', [])

        if not selected_num.isdigit() or int(selected_num) < 1 or int(selected_num) > len(admins):
            await update.message.reply_text(
                "❌ Invalid selection. Please choose a number from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_ADMIN_SELECT

        selected_admin = admins[int(selected_num) - 1]
        context.user_data['delete_selected'] = selected_admin

        await update.message.reply_text(
            f"⚠️ *Confirm Admin Removal*\n\n"
            f"*Admin Name:* {selected_admin['admin_name']}\n"
            f"*Chat ID:* {selected_admin['admin_chat_id']}\n"
            f"*Space:* {selected_admin['space_name']}\n\n"
            "This will revoke their admin access.\n\n"
            "Are you sure?\n\n"
            "✅ Yes - /submit\n"
            "❌ No - /cancel",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_ADMIN_CONFIRM

    except Exception as e:
        print(f"Error in handle_deleteadmin_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return DEL_ADMIN_SELECT


async def submit_deleteadmin(update: Update, context: CallbackContext) -> int:
    """Delete the admin from Google Sheets with verification"""
    try:
        admin = context.user_data.get('delete_selected')
        if not admin:
            await update.message.reply_text(
                "❌ No admin selected. Start over with /deleteadmin",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Verifying admin...")

        worksheet = init_google_sheets(ADMIN_SHEET)
        all_values = worksheet.get_all_values()

        # Verify admin exists
        admin_exists = False
        row_to_delete = None
        for i in range(len(all_values) - 1, 0, -1):  # Skip header
            row = all_values[i]
            if (len(row) >= 6 and
                    row[0] == str(update.message.chat_id) and
                    row[3] == admin['space_code'] and
                    row[4] == admin['admin_chat_id']):
                admin_exists = True
                row_to_delete = i + 1
                break

        if not admin_exists:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "❌ This admin no longer exists. The list has been refreshed.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Delete the row
        worksheet.delete_rows(row_to_delete)

        await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"✅ *Admin removed successfully!*\n\n"
            f"{admin['admin_name']} no longer has admin access to {admin['space_name']}.",
            parse_mode=ParseMode.MARKDOWN
        )
        await cleanup_messages(update, context, num_messages=3)

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_deleteadmin: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to remove admin: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def showadmin_command(update: Update, context: CallbackContext) -> None:
    """Show all admins grouped by space with clear separation"""
    try:
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your admins...")

        chat_id = str(update.message.chat_id)
        admins = get_user_admins(chat_id)

        await delete_loading_indicator(update, context)

        if not admins:
            await update.message.reply_text(
                "ℹ️ You haven't added any admins yet.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Group admins by space with counts
        space_admins = {}
        for admin in admins:
            space_key = f"{admin['space_name']} ({admin['space_code']})"
            if space_key not in space_admins:
                space_admins[space_key] = []
            space_admins[space_key].append(admin)

        response = "👥 *Your Team Admins*\n\n"
        for space, space_admins_list in space_admins.items():
            response += f"🚀 *{space}* ({len(space_admins_list)}/4 admins)\n"
            for admin in space_admins_list:
                response += f"  • {admin['admin_name']} (`{admin['admin_chat_id']}`)\n"
            response += "\n"

        response += "--------------------------------\n"
        response += "/addadmin - Add new admin\n"
        response += "/deleteadmin - Remove admin"

        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        print(f"Error in showadmin_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your admins. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )


async def get_user_reminders(chat_id: int, date_filter: str) -> dict:
    """Get reminders for a user based on date filter (today/tomorrow/thisweek)"""
    try:
        worksheet = init_google_sheets(REMINDERS_ROOT_SHEET)
        all_values = worksheet.get_all_values()

        # Skip header rows by checking if first column contains 'chatid' or similar
        header_rows = 1  # Default to skip 1 row
        if len(all_values) > 0 and any('chat' in str(cell).lower() for cell in all_values[0]):
            header_rows = 1  # Skip one header row
        elif len(all_values) > 1 and any('chat' in str(cell).lower() for cell in all_values[1]):
            header_rows = 2  # Skip two header rows

        all_reminders = all_values[header_rows:]  # Skip header rows

        today = datetime.datetime.now(PH_TZ).date()
        tomorrow = today + timedelta(days=1)

        reminders = {
            'Once': [],
            'Daily': [],
            'Weekly': [],
            'Monthly': [],
            'Yearly': []
        }

        for row in all_reminders:
            if len(row) < 20:  # Ensure row has all needed columns
                continue

            # Get reminder details
            try:
                # Skip if date is a header
                if row[3] in ['Rem Date', 'Start Date', 'Date']:
                    continue

                # Safely parse date
                try:
                    reminder_date = datetime.datetime.strptime(row[3], "%m/%d/%Y").date()  # Column D
                except ValueError:
                    # Try alternative date formats if needed
                    try:
                        reminder_date = datetime.datetime.strptime(row[3], "%Y-%m-%d").date()
                    except:
                        print(f"Skipping row with invalid date format: {row[3]}")
                        continue

                time_str = row[4]  # Column E
                recurrence = row[5]  # Column F
                message = row[6]  # Column G
                member_name = row[2]  # Column C
                project_name = row[8] if len(row) > 8 else ""  # Column I

                # Check if user has access (creator or manager/co-creator)
                has_access = False
                if row[0] == str(chat_id):  # Column A - creator
                    has_access = True
                elif str(chat_id) in [row[18], row[19], row[20], row[21],
                                      row[22]]:  # Columns S,T,U,V,W (0-indexed as 18-22)
                    has_access = True

                if not has_access:
                    continue

                # Apply date filter
                matches_filter = False
                if date_filter == "today":
                    if recurrence == "Once":
                        matches_filter = (reminder_date == today)
                    elif recurrence == "Daily":
                        matches_filter = True
                    elif recurrence == "Weekly":
                        matches_filter = (reminder_date.weekday() == today.weekday())
                    elif recurrence == "Monthly":
                        matches_filter = (reminder_date.day == today.day)
                    elif recurrence == "Yearly":
                        matches_filter = (reminder_date.month == today.month and
                                          reminder_date.day == today.day)
                elif date_filter == "tomorrow":
                    if recurrence == "Once":
                        matches_filter = (reminder_date == tomorrow)
                    elif recurrence == "Daily":
                        matches_filter = True
                    elif recurrence == "Weekly":
                        matches_filter = (reminder_date.weekday() == tomorrow.weekday())
                    elif recurrence == "Monthly":
                        matches_filter = (reminder_date.day == tomorrow.day)
                    elif recurrence == "Yearly":
                        matches_filter = (reminder_date.month == tomorrow.month and
                                          reminder_date.day == tomorrow.day)
                elif date_filter == "thisweek":
                    week_start = today - timedelta(days=today.weekday())
                    week_end = week_start + timedelta(days=6)

                    if recurrence == "Once":
                        matches_filter = (week_start <= reminder_date <= week_end)
                    elif recurrence == "Daily":
                        matches_filter = True
                    elif recurrence == "Weekly":
                        matches_filter = True  # All weekly recurrences count in week
                    elif recurrence == "Monthly":
                        matches_filter = (week_start.day <= reminder_date.day <= week_end.day)
                    elif recurrence == "Yearly":
                        year_day = (reminder_date.month, reminder_date.day)
                        week_days = [(d.month, d.day) for d in
                                     (week_start + timedelta(days=i) for i in range(7))]
                        matches_filter = year_day in week_days

                # Modified condition to exclude past-due ONLY for "Once" reminders
                if matches_filter and (recurrence != "Once" or reminder_date >= today):
                    reminders[recurrence].append({
                        'date': reminder_date.strftime("%m/%d/%Y"),
                        'time': time_str,
                        'message': message,
                        'member': member_name,
                        'project': project_name
                    })

            except Exception as e:
                print(f"Error processing row: {e}")
                continue

        return reminders

    except Exception as e:
        print(f"Error in get_user_reminders: {e}")
        return None


async def format_reminders_response(reminders: dict, date_filter: str = "today") -> str:
    """Format reminders dictionary into the required output format, skipping empty categories"""
    # Determine header based on date filter
    if date_filter == "today":
        header = "⏰ *TODAY'S SCHEDULE*"
    elif date_filter == "tomorrow":
        header = "⏰ *TOMORROW'S SCHEDULE*"
    elif date_filter == "thisweek":
        today = datetime.datetime.now(PH_TZ).date()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        header = f"⏰ *WEEKLY SCHEDULE* ({week_start.strftime('%m/%d')}-{week_end.strftime('%m/%d')})"
    else:
        header = "⏰ *SCHEDULE*"

    response = f"{header}\n"
    response += "-------------------------------------\n"

    # Emoji mapping for each recurrence type
    emoji_map = {
        'Once': '1️⃣',
        'Daily': '☀️',
        'Weekly': '🌤',
        'Monthly': '🗓',
        'Yearly': '📆'
    }

    has_reminders = False
    current_time = datetime.datetime.now(PH_TZ)

    for recurrence in ['Once', 'Daily', 'Weekly', 'Monthly', 'Yearly']:
        if reminders[recurrence]:
            has_reminders = True
            # Sort reminders by time
            sorted_reminders = sorted(
                reminders[recurrence],
                key=lambda x: datetime.datetime.strptime(
                    x['time'].replace('.', ''),
                    "%I:%M %p"
                ).time()
            )

            response += f"\n{emoji_map[recurrence]} *{recurrence.upper()}*\n\n"

            for rem in sorted_reminders:
                # Check if the reminder time has passed
                reminder_time = datetime.datetime.strptime(rem['time'], "%I:%M %p").time()
                current_time_only = current_time.time()

                # For all recurrence types, check if time has passed today
                is_past = current_time_only > reminder_time

                # For Once reminders, also check if date has passed
                if recurrence == 'Once':
                    reminder_date = datetime.datetime.strptime(rem['date'], "%m/%d/%Y").date()
                    if current_time.date() > reminder_date:
                        is_past = True
                    elif current_time.date() < reminder_date:
                        is_past = False

                # New format:
                # • _Time | Project | Name_ (entire first row in italics)
                # ▪️ *Schedule Message* (bold)
                response += (
                    f"• _{rem['time']} | {rem['project']} | {rem['member']}_\n"
                    f"▪️ *{rem['message']}*\n\n"
                )

            response += "-------------------------------------\n"

    if not has_reminders:
        return "No reminders scheduled."

    # Only add the prompt if there are reminders
    if any(reminders.values()):
        response += "\n/monitoring - Check more."

    return response


async def schedtoday_command(update: Update, context: CallbackContext) -> None:
    """Show today's schedule"""
    try:
        loading_msg = await show_loading_indicator(update, context, "⏳ Loading today's schedule...")
        reminders = await get_user_reminders(update.message.chat_id, "today")

        response = await format_reminders_response(reminders, "today")

        # Only add the "addrem" prompt if there are reminders
        if any(reminders.values()):
            response += "\n/addsched - Add schedule."

        await delete_loading_indicator(update, context)
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
        # await cleanup_messages(update, context, num_messages=1)

    except Exception as e:
        print(f"Error in schedtoday_command: {e}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading today's schedule. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )


async def schedtomorrow_command(update: Update, context: CallbackContext) -> None:
    """Show tomorrow's schedule"""
    try:
        loading_msg = await show_loading_indicator(update, context, "⏳ Loading tomorrow's schedule...")
        reminders = await get_user_reminders(update.message.chat_id, "tomorrow")

        response = await format_reminders_response(reminders, "tomorrow")

        if not any(reminders.values()):
            await update.message.reply_text("No reminders scheduled for tomorrow.")
            return

        response += "\n/addsched - Add schedule."

        await delete_loading_indicator(update, context)
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
        # await cleanup_messages(update, context, num_messages=1)

    except Exception as e:
        print(f"Error in schedtomorrow_command: {e}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading tomorrow's schedule. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )


async def schedthisweek_command(update: Update, context: CallbackContext) -> None:
    """Show this week's schedule"""
    try:
        loading_msg = await show_loading_indicator(update, context, "⏳ Loading this week's schedule...")
        reminders = await get_user_reminders(update.message.chat_id, "thisweek")

        response = await format_reminders_response(reminders, "thisweek")

        if not any(reminders.values()):
            await update.message.reply_text("No schedules scheduled for this week.")
            return

        response += "\n/addsched - Add schedule."

        await delete_loading_indicator(update, context)
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
        # await cleanup_messages(update, context, num_messages=1)

    except Exception as e:
        print(f"Error in schedthisweek_command: {e}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading this week's schedule. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )


async def showmember_command(update: Update, context: CallbackContext) -> int:
    """Show all members grouped by space with letter-based selection"""
    try:
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your team members...")

        # Check if user is a manager or admin
        chat_id = str(update.message.chat_id)

        # Get all spaces this user manages
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        manager_spaces = {}
        for row in manager_rows[1:]:  # Skip header
            if len(row) >= 5 and row[0] == chat_id:  # Check chat_id in column A
                space_code = row[3]  # Column D
                space_name = row[4] if len(row) > 4 else 'Unnamed Space'  # Column E
                manager_spaces[space_code] = space_name

        # Get all spaces this user is admin of
        admin_spaces = {}
        admin_sheet = init_google_sheets(ADMIN_SHEET)
        admin_rows = admin_sheet.get_all_values()
        for row in admin_rows[1:]:  # Skip header
            if len(row) >= 6 and row[4] == chat_id:  # Column E is admin_chat_id
                space_code = row[3]  # Column D
                if space_code not in manager_spaces:  # Don't duplicate if already manager
                    space_name = get_space_name(space_code)
                    admin_spaces[space_code] = space_name

        if not manager_spaces and not admin_spaces:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "❌ Only managers/admins can view members list.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Get all members in these spaces
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        all_members = members_sheet.get_all_values()

        # {space_code: {'name': space_name, 'members': [{'name': str, 'chat_id': str}]}}
        space_members = {}
        total_members = 0

        for row in all_members[1:]:  # Skip header
            if len(row) >= 5 and row[3] in {**manager_spaces, **admin_spaces}:  # Column D is space code
                space_code = row[3]
                member_name = row[2]
                member_chat_id = row[0]

                if space_code not in space_members:
                    space_members[space_code] = {
                        'name': manager_spaces.get(space_code, admin_spaces.get(space_code)),
                        'members': []
                    }

                space_members[space_code]['members'].append({
                    'name': member_name,
                    'chat_id': member_chat_id
                })
                total_members += 1

        await delete_loading_indicator(update, context)

        if not space_members:
            await update.message.reply_text(
                "ℹ️ You don't have any members in your spaces yet.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the members list with continuous lowercase letter-based commands
        response = f"👥 *Overall Total Members:* {total_members}\n\n"
        member_counter = 0
        member_mapping = {}  # To store member details by letter

        # Generate all letters first based on total members
        all_letters = [chr(97 + i) for i in range(total_members)]  # a, b, c... for all members

        letter_index = 0
        for space_code, space_data in space_members.items():
            response += f"🚀 *{space_data['name']}* (`{space_code}`)\n"

            for member in space_data['members']:
                if letter_index >= len(all_letters):
                    break  # Just in case we have more members than letters

                letter = all_letters[letter_index]
                member_mapping[letter] = {
                    'chat_id': member['chat_id'],
                    'name': member['name'],
                    'space_code': space_code,
                    'space_name': space_data['name']
                }
                response += f"- /{letter} 👤 {member['name']}\n"
                letter_index += 1

            response += "\n"

        response += "--------------------------------\n"
        response += "Tap a letter to view member's schedules\n"
        response += "/deletemember - Remove a member\n"

        # Store member mapping for later use
        context.user_data['member_mapping'] = member_mapping
        # Add context marker that we're in showmember mode
        context.user_data['selection_mode'] = 'showmember'

        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN
        )

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in showmember_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your team members. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def view_member_schedules(update: Update, context: CallbackContext) -> None:
    """Handle when a manager clicks on a member letter to view their schedules"""
    try:
        # Only process if we're in showmember mode
        if context.user_data.get('selection_mode') != 'showmember':
            return

        command = update.message.text.strip().lower()
        if not command.startswith('/'):
            return

        member_letter = command[1:]  # Remove the leading '/'
        member_mapping = context.user_data.get('member_mapping', {})

        if member_letter not in member_mapping:
            await update.message.reply_text(
                "❌ Invalid member selection.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        member_info = member_mapping[member_letter]
        loading_msg = await show_loading_indicator(update, context, f"🔍 Loading {member_info['name']}'s schedules...")

        # Get all reminders for this member in this specific space
        reminders_sheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        all_reminders = reminders_sheet.get_all_values()

        member_reminders = []
        for row in all_reminders[1:]:  # Skip header
            if len(row) >= 9 and row[0] == member_info['chat_id'] and row[8] != "General":
                # Check if this reminder belongs to a project in the selected space
                projects_sheet = init_google_sheets("Projects")
                project_row = projects_sheet.find(row[8])  # Column I is project name
                if project_row:
                    project_space = projects_sheet.cell(project_row.row, 4).value  # Column D is space code
                    if project_space == member_info['space_code']:
                        member_reminders.append({
                            'date': row[3],
                            'time': row[4],
                            'recurrence': row[5],
                            'text': row[6],
                            'project': row[8]
                        })

        await delete_loading_indicator(update, context)

        if not member_reminders:
            await update.message.reply_text(
                f"ℹ️ {member_info['name']} has no schedules in {member_info['space_name']}.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Format the response similar to schedtoday with new format
        response = (
            f"⏰ *{member_info['name']}'s Schedules in {member_info['space_name']}*\n"
            "-------------------------------------\n"
        )

        # Group by recurrence type
        reminders_by_type = {
            'Once': [],
            'Daily': [],
            'Weekly': [],
            'Monthly': [],
            'Yearly': []
        }

        for rem in member_reminders:
            reminders_by_type[rem['recurrence']].append(rem)

        # Emoji mapping for each recurrence type
        emoji_map = {
            'Once': '1️⃣',
            'Daily': '☀️',
            'Weekly': '🌤',
            'Monthly': '🗓',
            'Yearly': '📆'
        }

        for recurrence, reminders in reminders_by_type.items():
            if reminders:
                # Sort by time
                sorted_reminders = sorted(
                    reminders,
                    key=lambda x: datetime.datetime.strptime(
                        x['time'].replace('.', ''),
                        "%I:%M %p"
                    ).time()
                )

                response += f"\n{emoji_map[recurrence]} *{recurrence.upper()}*\n\n"
                for rem in sorted_reminders:
                    response += (
                        f"• _{rem['time']} | {rem['project']}_\n"
                        f"▪️ *{rem['text']}*\n\n"
                    )

        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        print(f"Error in view_member_schedules: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading member schedules. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )


async def deletemember_command(update: Update, context: CallbackContext) -> int:
    """Start the member deletion process - shows manager's spaces"""
    try:
        context.user_data.clear()
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your spaces...")

        chat_id = str(update.message.chat_id)

        # Check if user is manager or co-admin
        is_manager = False
        is_admin = False

        # Check if manager
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        manager_spaces = {}
        for row in manager_rows[1:]:  # Skip header
            if row[0] == chat_id and len(row) >= 5:
                space_code = row[3]
                space_name = row[4] if len(row) > 4 else 'Unnamed Space'
                manager_spaces[space_code] = space_name
                is_manager = True

        # If not manager, check if admin
        if not is_manager:
            admin_sheet = init_google_sheets(ADMIN_SHEET)
            admin_rows = admin_sheet.get_all_values()
            for row in admin_rows[1:]:  # Skip header
                if row[4] == chat_id and len(row) >= 6:  # Column E is admin_chat_id
                    space_code = row[3]
                    space_name = get_space_name(space_code)
                    manager_spaces[space_code] = space_name
                    is_admin = True

        if not is_manager and not is_admin:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "❌ Only managers or admins can remove members.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        await delete_loading_indicator(update, context)

        if not manager_spaces:
            await update.message.reply_text(
                "ℹ️ You don't manage any spaces yet.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format spaces list with /commands
        spaces_list = "\n".join(
            f"/{code} - {name}"
            for code, name in manager_spaces.items()
        )

        context.user_data['delete_member_spaces'] = manager_spaces
        context.user_data['is_manager'] = is_manager

        # Add context marker that we're in deletemember mode
        context.user_data['selection_mode'] = 'deletemember'

        await update.message.reply_text(
            "🗑 *Remove Member From Space*\n\n"
            "Which space contains the member you want to remove?\n\n"
            f"{spaces_list}\n\n"
            "Tap on the space code above to select it\n\n"
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_PROJECT_SELECT

    except Exception as e:
        print(f"Error in deletemember_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your spaces. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_deletemember_space(update: Update, context: CallbackContext) -> int:
    try:
        selected_code_with_slash = update.message.text.strip()
        selected_code = selected_code_with_slash.lstrip('/').upper()
        spaces = context.user_data.get('delete_member_spaces', {})

        if selected_code not in spaces:
            await update.message.reply_text(
                "❌ Invalid space selection. Please choose from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_PROJECT_SELECT

        loading_msg = await show_loading_indicator(update, context, "🔍 Loading members...")

        # Get all members in this space
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        all_members = members_sheet.get_all_values()

        space_members = []
        member_letters = []  # Store letters for mapping
        for row in all_members[1:]:  # Skip header
            if len(row) >= 5 and row[3] == selected_code:  # Column D is space code
                # Don't allow removing yourself
                if row[0] != str(update.message.chat_id):
                    space_members.append({
                        'chat_id': row[0],
                        'name': row[2]
                    })

        await delete_loading_indicator(update, context)

        if not space_members:
            await update.message.reply_text(
                f"ℹ️ No removable members found in {spaces[selected_code]}.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # THIS WAS THE PROBLEM AREA - FIXED INDENTATION BELOW
        # Format members list with UPPERCASE letter commands (A, B, C...)
        member_letters = [chr(65 + i) for i in range(len(space_members))]  # A, B, C...
        members_list = "\n".join(
            f"/{letter} - {member['name']}"
            for letter, member in zip(member_letters, space_members)
        )

        # Store data for confirmation - include letter mapping
        context.user_data['delete_member_list'] = {
            letter: member for letter, member in zip(member_letters, space_members)
        }
        context.user_data['selected_space'] = {
            'code': selected_code,
            'name': spaces[selected_code]
        }
        # Mark that we're in uppercase deletemember mode
        context.user_data['selection_mode'] = 'deletemember_uppercase'

        await update.message.reply_text(
            "👥 *Select Member to Remove*\n\n"
            f"{members_list}\n\n"
            "Tap on the LETTER above to select a member\n\n"
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_PROJECT_CONFIRM

    except Exception as e:
        print(f"Error in handle_deletemember_space: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "❌ Error loading members. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return DEL_PROJECT_SELECT


async def handle_deletemember_select(update: Update, context: CallbackContext) -> int:
    """Handle UPPERCASE member selection and show confirmation"""
    try:
        command = update.message.text.strip()
        if not command.startswith('/'):
            await update.message.reply_text(
                "❌ Please select a member using the LETTER commands.",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_PROJECT_CONFIRM

        member_letter = command[1:]  # Remove leading '/'
        members = context.user_data.get('delete_member_list', {})
        space_info = context.user_data.get('selected_space', {})

        # Only accept uppercase letters
        if not member_letter.isupper() or member_letter not in members:
            await update.message.reply_text(
                "❌ Invalid selection. Please choose a LETTER from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_PROJECT_CONFIRM

        selected_member = members[member_letter]
        context.user_data['selected_member'] = selected_member

        await update.message.reply_text(
            f"⚠️ *Confirm Member Removal*\n\n"
            f"*Member:* {selected_member['name']}\n"
            f"*Space:* {space_info['name']}\n\n"
            "This will:\n"
            "- Remove them from the space\n"
            "- Remove any admin privileges\n"
            "- Delete all their schedules in this space\n\n"
            "Are you sure?\n\n"
            "✅ Yes - /submit\n"
            "❌ No - /cancel",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_PROJECT_CONFIRM

    except Exception as e:
        print(f"Error in handle_deletemember_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return DEL_PROJECT_CONFIRM


async def submit_deletemember(update: Update, context: CallbackContext) -> int:
    """Delete the member and all related data"""
    try:
        member = context.user_data.get('selected_member')
        space_info = context.user_data.get('selected_space')
        is_manager = context.user_data.get('is_manager', False)

        if not member or not space_info:
            await update.message.reply_text(
                "❌ No member selected. Start over with /deletemember",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Removing member...")

        # 1. Remove from Members sheet
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        all_members = members_sheet.get_all_values()
        rows_deleted = 0

        for i in range(len(all_members) - 1, 0, -1):  # Skip header, search bottom-up
            row = all_members[i]
            if (len(row) >= 5 and
                    row[0] == member['chat_id'] and
                    row[3] == space_info['code']):
                members_sheet.delete_rows(i + 1)  # Rows are 1-indexed
                rows_deleted += 1

        if rows_deleted == 0:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                f"ℹ️ {member['name']} is not a member of {space_info['name']}.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # 2. Remove admin privileges if they were admin
        admin_rows_deleted = 0
        if is_manager:  # Only managers can remove admins
            admin_sheet = init_google_sheets(ADMIN_SHEET)
            all_admins = admin_sheet.get_all_values()

            for i in range(len(all_admins) - 1, 0, -1):  # Skip header, search bottom-up
                row = all_admins[i]
                if (len(row) >= 6 and
                        row[4] == member['chat_id'] and
                        row[3] == space_info['code']):
                    admin_sheet.delete_rows(i + 1)
                    admin_rows_deleted += 1

        # 3. Delete their schedules in this space
        reminders_deleted = 0

        # First get all projects in this space
        projects_sheet = init_google_sheets("Projects")
        all_projects = projects_sheet.get_all_values()
        space_projects = set()

        for row in all_projects[1:]:  # Skip header
            if len(row) >= 4 and row[3] == space_info['code']:  # Column D is space code
                project_name = row[5] if len(row) > 5 else "Unnamed Project"
                space_projects.add(project_name)

        # Now delete reminders
        reminders_sheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        all_reminders = reminders_sheet.get_all_values()

        for i in range(len(all_reminders) - 1, 0, -1):  # Skip header, search bottom-up
            row = all_reminders[i]
            if (len(row) >= 9 and
                    row[0] == member['chat_id'] and
                    row[8] in space_projects):  # Column I is project name
                reminders_sheet.delete_rows(i + 1)
                reminders_deleted += 1

        # 4. Clear timestamps in RemindersRoot
        reminders_root = init_google_sheets("RemindersRoot")
        reminders_root.batch_clear(["M3:M", "AA3:AA"])

        # Notify member if possible
        try:
            await context.bot.send_message(
                chat_id=int(member['chat_id']),
                text=f"❌ *Removed from Space*\n\n"
                     f"You have been removed from *{space_info['name']}*.\n\n"
                     "All your schedules in this space have been deleted.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying member: {e}")

        await delete_loading_indicator(update, context)

        response = (
            f"✅ *Member removed successfully!*\n\n"
            f"*Name:* {member['name']}\n"
            f"*Space:* {space_info['name']}\n\n"
            "*Removed:*\n"
            f"- Member record\n"
            f"- {admin_rows_deleted} admin privileges\n"
            f"- {reminders_deleted} schedules\n\n"
            "/showmember - View remaining members"
        )

        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
        await cleanup_messages(update, context, num_messages=4)

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_deletemember: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to remove member: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def assignrem_command(update: Update, context: CallbackContext) -> int:
    """Start the schedule assignment process for managers"""
    try:
        context.user_data.clear()
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your team members...")

        # Check if user is a manager
        worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        chat_id = str(update.message.chat_id)
        all_values = worksheet.get_all_values()

        is_manager = any(row[0] == chat_id for row in all_values[1:])  # Skip header

        if not is_manager:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "❌ Only managers can assign schedules.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Get all spaces this manager owns
        manager_spaces = {}
        for row in all_values[1:]:  # Skip header
            if row[0] == chat_id and len(row) >= 5:  # Check chat_id in column A
                space_code = row[3]  # Column D
                space_name = row[4] if len(row) > 4 else 'Unnamed Space'  # Column E
                manager_spaces[space_code] = space_name

        if not manager_spaces:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "ℹ️ You haven't created any spaces yet. Create one first with /addspace",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Get all projects in these spaces
        projects_sheet = init_google_sheets("Projects")
        all_projects = projects_sheet.get_all_values()

        space_codes = list(manager_spaces.keys())
        member_projects = set()
        for row in all_projects[1:]:  # Skip header
            if len(row) > 3 and row[3] in space_codes:  # Column D is space code
                project_name = row[5] if len(row) > 5 else "Unnamed Project"
                member_projects.add(project_name)

        if not member_projects:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "❌ You don't have any projects in your spaces yet. Create projects first with /addproject.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Get all members in these spaces
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        all_members = members_sheet.get_all_values()

        # {chat_id: {'name': str, 'spaces': [str]}}
        members_info = {}
        for row in all_members[1:]:  # Skip header
            if len(row) >= 5 and row[3] in manager_spaces:  # Column D is space code
                member_chat_id = row[0]
                member_name = row[2]
                space_name = manager_spaces[row[3]]

                if member_chat_id not in members_info:
                    members_info[member_chat_id] = {
                        'name': member_name,
                        'spaces': [space_name]
                    }
                else:
                    members_info[member_chat_id]['spaces'].append(space_name)

        await delete_loading_indicator(update, context)

        if not members_info:
            await update.message.reply_text(
                "ℹ️ You don't have any members in your spaces yet.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the members list with assign commands
        members_list = []
        for chat_id, info in members_info.items():
            escaped_chat_id = str(chat_id).replace("_", "\\_")
            escaped_name = info['name'].replace("-", "\\-").replace("_", "\\_").replace(".", "\\.")
            escaped_spaces = ", ".join(info['spaces']).replace("-", "\\-").replace("_", "\\_")

            members_list.append(
                f"ㆍ /assignremto\\_{escaped_chat_id}\n"
                f"Name: {escaped_name} \\| {escaped_spaces}\n"
            )

        context.user_data['assign_members'] = members_info
        context.user_data['manager_spaces'] = manager_spaces
        context.user_data['member_projects'] = sorted(member_projects)

        await update.message.reply_text(
            "🙏 *Assign Schedule To?*\n\n"
            "Please select a team member to assign a schedule to:\n\n"
            + "\n".join(members_list) + "\n"
                                        "/cancel - Cancel assigning.",
            parse_mode=ParseMode.MARKDOWN
        )

        return ASSIGN_REM_SELECT_MEMBER

    except Exception as e:
        print(f"Error in assignrem_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your team members. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_assignrem_select(update: Update, context: CallbackContext) -> int:
    """Handle selected member for schedule assignment"""
    try:
        command = update.message.text.strip()
        if not command.startswith('/assignremto_'):
            await update.message.reply_text(
                "❌ Please select a member using the provided commands.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ASSIGN_REM_SELECT_MEMBER

        # Extract chat ID from command
        member_chat_id = command.split('_')[1]
        members_info = context.user_data.get('assign_members', {})

        if member_chat_id not in members_info:
            await update.message.reply_text(
                "❌ Invalid member selection. Please try again or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return ASSIGN_REM_SELECT_MEMBER

        # Store selected member
        context.user_data['assign_selected'] = {
            'chat_id': member_chat_id,
            'name': members_info[member_chat_id]['name']
        }

        await update.message.reply_text(
            "📝 *Create Schedule for Member*\n\n"
            f"Member: {members_info[member_chat_id]['name']}\n"
            f"Chat ID: {member_chat_id}\n\n"
            " *Please enter the schedule in this format:*\n\n"
            "`Date, Time, Recurrence, Reminder Text`\n\n"
            "*Example:*\n"
            "`6/21/25, 8:00 PM, O, Meeting`\n\n"

            "*Note:*\n"
            "- Recurrence: Once/Daily/Weekly/Monthly/Yearly (or first letter)\n\n"
            "Type /cancel to stop.",
            parse_mode=ParseMode.MARKDOWN
        )

        return ASSIGN_REM_INPUT

    except Exception as e:
        print(f"Error in handle_assignrem_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASSIGN_REM_SELECT_MEMBER


async def handle_assignrem_input(update: Update, context: CallbackContext) -> int:
    """Process the reminder input for assigned member"""
    try:
        text = update.message.text.strip()
        if text.startswith('/'):
            await update.message.reply_text("❌ Please enter your reminder first.")
            return ASSIGN_REM_INPUT

        loading_msg = await show_loading_indicator(update, context, "🔍 Processing...")

        parts = [p.strip() for p in re.split(r'[,;]', text)]
        if len(parts) < 4:
            await delete_loading_indicator(update, context)
            raise ValueError("Not enough parts (need date, time, recurrence, and text)")

        # Parse date
        date_part = parts[0]
        month, day, year = parse_flexible_date(date_part)
        formatted_date = f"{month}/{day}/{year}"
        reminder_date = datetime.date(year, month, day)
        date_display = reminder_date.strftime("%B %d, %Y")
        weekday = reminder_date.strftime("%A")

        # Parse time
        time_part = parts[1]
        formatted_time = parse_flexible_time(time_part)

        # Parse recurrence
        recurrence_part = parts[2].strip().upper()[0]
        recurrence_map = {
            'O': 'Once',
            'D': 'Daily',
            'W': 'Weekly',
            'M': 'Monthly',
            'Y': 'Yearly'
        }
        if recurrence_part not in recurrence_map:
            await delete_loading_indicator(update, context)
            raise ValueError("Invalid recurrence. Use: Once/Daily/Weekly/Monthly/Yearly")
        recurrence_word = recurrence_map[recurrence_part]

        # Get reminder text
        reminder_text = ','.join(parts[3:]).strip()
        if not reminder_text:
            await delete_loading_indicator(update, context)
            raise ValueError("Reminder text cannot be empty")

        # Generate ID (using member's chat ID)
        member_chat_id = int(context.user_data['assign_selected']['chat_id'])
        reminder_id = await get_next_reminder_id(member_chat_id)

        # Store data
        context.user_data['assign_reminder'] = {
            'date': formatted_date,
            'date_display': date_display,
            'time': formatted_time,
            'recurrence': recurrence_part,
            'recurrence_word': recurrence_word,
            'text': reminder_text,
            'id': reminder_id,
            'weekday': weekday,
            'day': day,
            'month': month
        }

        await delete_loading_indicator(update, context)

        # Get user's projects - BOTH AS MANAGER AND MEMBER
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your projects...")

        # Get projects for this member's spaces - REMOVED GENERAL OPTION
        manager_spaces = context.user_data.get('manager_spaces', {})
        space_codes = list(manager_spaces.keys())

        projects_sheet = init_google_sheets("Projects")
        all_projects = projects_sheet.get_all_values()

        member_projects = []
        for row in all_projects[1:]:  # Skip header
            if len(row) > 3 and row[3] in space_codes:  # Column D is space code
                project_name = row[5] if len(row) > 5 else "Unnamed Project"
                member_projects.append(project_name)

        if not member_projects:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                "❌ No projects found in your spaces. Create projects first with /addproject.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Sort projects
        sorted_projects = sorted(member_projects)
        context.user_data['assign_projects'] = {
            str(i + 1): project for i, project in enumerate(sorted_projects)
        }

        # Format project list
        projects_list = []
        for i, project in enumerate(sorted_projects, start=1):
            projects_list.append(f"/{i} - {project}")

        await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "📂 *Select a project for this reminder:*\n\n" +
            "\n".join(projects_list) + "\n\n" +
            "Tap on the number above to select it\n\n" +
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASSIGN_REM_PROJECT_SELECT

    except Exception as e:
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"❌ Error: {str(e)}\n\n"
            "Please use format:\n"
            "`<date>, <time>, <recurrence>, <your text>`\n\n"
            "*Example:* `June 21 2025, 10:00 AM, W, Weekly meeting`\n\n"
            "Try again or /cancel",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASSIGN_REM_INPUT


async def handle_assignrem_project(update: Update, context: CallbackContext) -> int:
    """Handle project selection for assigned reminder"""
    try:
        command = update.message.text.strip()
        if not command.startswith('/'):
            await update.message.reply_text(
                "❌ Please select a project using the numbered commands.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ASSIGN_REM_PROJECT_SELECT

        # Extract number from command
        selected_num = command[1:]  # Remove the leading '/'
        projects = context.user_data.get('assign_projects', {})

        if selected_num not in projects:
            await update.message.reply_text(
                "❌ Invalid selection. Please choose a number from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return ASSIGN_REM_PROJECT_SELECT

        # Store selected project
        context.user_data['assign_reminder']['project'] = projects[selected_num]
        return await show_assignrem_confirmation(update, context)

    except Exception as e:
        print(f"Error in handle_assignrem_project: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing project selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ASSIGN_REM_PROJECT_SELECT


async def show_assignrem_confirmation(update: Update, context: CallbackContext) -> int:
    """Show confirmation for assigned reminder"""
    reminder = context.user_data.get('assign_reminder', {})
    member_info = context.user_data.get('assign_selected', {})

    confirmation_msg = (
        f"🙏 *Please confirm reminder for {member_info.get('name', 'member')}:*\n\n"
        f"*Date:* {reminder.get('date_display', '')}\n"
        f"*Time:* {reminder.get('time', '')}\n"
        f"*Recurrence:* {reminder.get('recurrence_word', '')}\n"
        f"*Reminder:* {reminder.get('text', '')}\n"
        f"*Project:* {reminder.get('project', 'General')}\n\n"
        # f"*ID#:* _{reminder.get('id', '')}_\n\n"
        "/submit - Confirm.\n"
        "/cancel - Cancel assigning schedule."
    )

    await update.message.reply_text(
        confirmation_msg,
        parse_mode=ParseMode.MARKDOWN
    )

    return ASSIGN_REM_CONFIRM


async def submit_assignrem(update: Update, context: CallbackContext) -> int:
    """Save the assigned reminder and notify member"""
    try:
        reminder = context.user_data.get('assign_reminder')
        member_info = context.user_data.get('assign_selected')

        if not reminder or not member_info:
            await update.message.reply_text(
                "❌ No reminder data found. Start over with /assignrem",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Assigning schedule...")

        # Save to Added Reminders sheet with member's chat ID
        worksheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
        row_data = [
            member_info['chat_id'],  # Member's chat ID
            timestamp,
            member_info['name'],  # Member's name
            reminder['date'],
            reminder['time'],
            reminder['recurrence_word'],
            reminder['text'],
            str(reminder['id']),
            reminder.get('project', 'General')
        ]
        worksheet.append_row(row_data)

        # Update ID tracker for member
        id_tracker = init_reminder_id_tracker()
        cell = id_tracker.find(str(member_info['chat_id']))

        if cell:
            id_tracker.update_cell(cell.row, 2, reminder['id'])
        else:
            id_tracker.append_row([member_info['chat_id'], reminder['id']])

        # Update cache
        if 'reminders' not in id_cache:
            id_cache['reminders'] = {}
        id_cache['reminders'][int(member_info['chat_id'])] = reminder['id']

        await delete_loading_indicator(update, context)

        # Notify member
        try:
            await context.bot.send_message(
                chat_id=int(member_info['chat_id']),
                text=f"🔔 *New Assigned Schedule!*\n\n"
                     f"Your manager has assigned you a new schedule:\n\n"
                     f"*Date:* {reminder['date_display']}\n"
                     f"*Time:* {reminder['time']}\n"
                     f"*Recurrence:* {reminder['recurrence_word']}\n"
                     f"*Schedule:* {reminder['text']}\n"
                     f"*Project:* {reminder.get('project', 'General')}\n\n"
                # f"ID#: {reminder['id']}\n\n"
                     "View all schedules with /showsched",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying member: {e}")

        await update.message.reply_text(
            f"✅ *Schedule successfully assigned to {member_info['name']}!*\n\n"
            # f"ID#: {reminder['id']}\n\n"
            "/assignsched - Assign another schedule.\n"
            "/schedule - View your team schedules.",
            parse_mode=ParseMode.MARKDOWN
        )

        # Add cleanup here - this will delete previous messages in the conversation
        await cleanup_messages(update, context, num_messages=8)  # Adjust number as needed

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_assignrem: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to assign reminder: {str(e)}\n\n"
            "Please try again with /assignsched",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def suggestproject_command(update: Update, context: CallbackContext) -> int:
    """Start the project suggestion process"""
    try:
        # Clear any previous data
        context.user_data.clear()

        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your spaces...")

        # Get all spaces this member has joined
        worksheet = init_google_sheets(MEMBERS_SHEET)
        chat_id = str(update.message.chat_id)
        all_values = worksheet.get_all_values()

        # Find all unique spaces this member has joined (code + name)
        member_spaces = {}
        for row in all_values[1:]:  # Skip header
            if len(row) >= 5 and row[0] == chat_id:  # Check chat_id in column A
                code = row[3]  # Column D
                space_name = row[4] if len(row) > 4 else 'Unnamed Space'  # Column E
                member_spaces[code] = space_name

        await delete_loading_indicator(update, context)

        if not member_spaces:
            await update.message.reply_text(
                "ℹ️ You haven't joined any spaces yet. Join a space first or create space to make your own projects.\n\n"
                "/joinspace - Join existing space.\n"
                "/createspace - Join existing space.\n"
                "/addproject - Create a new project.\n",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the spaces list with / commands
        spaces_list = "\n".join(
            f"/{code} - {name}"  # Add / prefix to code
            for code, name in member_spaces.items()
        )

        # Store spaces data
        context.user_data['suggest_spaces'] = member_spaces

        await update.message.reply_text(
            "💡 *Suggest a New Project*\n\n"
            "Which space would you like to suggest a project for?\n\n"
            f"{spaces_list}\n\n"
            "Tap on the space code above to select it (it will auto-send)\n\n"
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return SUGGEST_SPACE_SELECT

    except Exception as e:
        print(f"Error in suggestproject_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your spaces. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_suggest_space_select(update: Update, context: CallbackContext) -> int:
    """Handle selected space for project suggestion"""
    try:
        selected_code_with_slash = update.message.text.strip()
        # Remove leading '/' if present
        selected_code = selected_code_with_slash.lstrip('/').upper()
        spaces = context.user_data.get('suggest_spaces', {})

        if selected_code not in spaces:
            await update.message.reply_text(
                "❌ Invalid code selection. Please choose from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return SUGGEST_SPACE_SELECT

        # Rest of the function remains the same...
        loading_msg = await show_loading_indicator(update, context, "🔍 Verifying space...")

        worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        all_values = worksheet.get_all_values()

        manager_info = None
        for row in all_values[1:]:  # Skip header
            if len(row) >= 5 and row[3].upper() == selected_code.upper():  # Column D is space code
                manager_info = {
                    'chat_id': row[0],
                    'name': row[2],
                    'space_name': row[4]
                }
                break

        await delete_loading_indicator(update, context)

        if not manager_info:
            await update.message.reply_text(
                "❌ Could not find the manager for this space. Please try another space.",
                parse_mode=ParseMode.MARKDOWN
            )
            return SUGGEST_SPACE_SELECT

        # Store selected space and manager info
        context.user_data['suggest_space_code'] = selected_code
        context.user_data['suggest_space_name'] = spaces[selected_code]
        context.user_data['manager_info'] = manager_info

        await update.message.reply_text(
            "📝 *What's the name of the project you want to suggest?*\n\n"
            "Examples:\n"
            "- Website Redesign\n"
            "- Marketing Campaign\n"
            "- Family Reunion Planning\n\n"
            "Enter the project name now or /cancel",
            parse_mode=ParseMode.MARKDOWN
        )

        return SUGGEST_PROJECT_NAME

    except Exception as e:
        print(f"Error in handle_suggest_space_select: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SUGGEST_SPACE_SELECT


async def handle_suggest_project_name(update: Update, context: CallbackContext) -> int:
    """Handle project name input and save to pending projects"""
    try:
        project_name = update.message.text.strip()
        if not project_name or len(project_name) > 100:
            await update.message.reply_text(
                "❌ Invalid project name! Please enter a name (max 100 characters).",
                parse_mode=ParseMode.MARKDOWN
            )
            return SUGGEST_PROJECT_NAME

        loading_msg = await show_loading_indicator(update, context, "⏳ Submitting your suggestion...")

        # Initialize pending projects sheet
        try:
            worksheet = init_google_sheets(PENDING_PROJECTS_SHEET)
            # Check if headers exist
            if not worksheet.get_values('A1:G1'):
                worksheet.update('A1:G1', [
                    ['ManagerChatID', 'Timestamp', 'MemberChatID', 'MemberName',
                     'SpaceCode', 'SpaceName', 'ProjectName', 'Status']
                ])
        except Exception as e:
            print(f"Error initializing Pending Projects sheet: {e}")
            raise

        # Save to pending projects sheet
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
        worksheet.append_row([
            context.user_data['manager_info']['chat_id'],
            timestamp,
            update.message.chat_id,
            update.message.from_user.full_name,
            context.user_data['suggest_space_code'],
            context.user_data['suggest_space_name'],
            project_name,
            "Pending"  # Status
        ])

        # Notify manager
        try:
            await context.bot.send_message(
                chat_id=int(context.user_data['manager_info']['chat_id']),
                text=f"💡 *New Project Suggestion*\n\n"
                     f"*From:* {update.message.from_user.full_name}\n"
                     f"*Space:* {context.user_data['suggest_space_name']} (`{context.user_data['suggest_space_code']}`)\n"
                     f"*Project:* {project_name}\n\n"
                     "To approve this suggestion:\n"
                     f"/approveproject_{update.message.chat_id}\n\n"
                     "To reject:\n"
                     f"/rejectproject_{update.message.chat_id}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying manager: {e}")

        await delete_loading_indicator(update, context)

        await update.message.reply_text(
            "✅ *Project suggestion submitted!*\n\n"
            f"Your suggestion for *{project_name}* in *{context.user_data['suggest_space_name']}* "
            "has been sent to the manager for approval.\n\n"
            "You'll receive a notification once reviewed. Check your status with /suggestprojectstatus.",
            parse_mode=ParseMode.MARKDOWN
        )

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in handle_suggest_project_name: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error submitting your suggestion. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SUGGEST_PROJECT_NAME


async def approve_project(update: Update, context: CallbackContext) -> None:
    """Approve a suggested project and add it with a hidden project code in Column G"""
    try:
        command = update.message.text
        # Extract member chat ID whether format is /approveproject123 or /approveproject_123
        member_chat_id = re.search(r'\d+', command).group()

        # Find the pending request
        pending_sheet = init_google_sheets(PENDING_PROJECTS_SHEET)
        all_requests = pending_sheet.get_all_values()

        request_info = None
        row_index = None

        for i, row in enumerate(all_requests[1:], start=2):  # Skip header
            if (row[0] == str(update.message.chat_id) and  # Manager's chat ID
                    row[2] == member_chat_id and  # Member's chat ID
                    row[7].lower() == "pending"):  # Status
                request_info = {
                    'member_chat_id': row[2],
                    'member_name': row[3],
                    'space_code': row[4],
                    'space_name': row[5],
                    'project_name': row[6],
                    'timestamp': row[1]
                }
                row_index = i
                break

        if not request_info:
            await update.message.reply_text("❌ No pending project suggestion found for this user.")
            return

        # Generate a random 4-character project code (hidden from users)
        project_code = generate_space_code()

        # Add to Projects sheet with all required columns including the hidden code
        projects_sheet = init_google_sheets("Projects")
        projects_sheet.append_row([
            update.message.chat_id,  # Column A - manager's chat ID
            request_info['timestamp'],  # Column B - original timestamp
            update.message.from_user.full_name,  # Column C - manager's name
            request_info['space_code'],  # Column D - space code
            request_info['space_name'],  # Column E - space name
            request_info['project_name'],  # Column F - project name
            project_code  # Column G - hidden project code
        ])

        # Update status in Pending sheet
        pending_sheet.update_cell(row_index, 8, "Approved")  # Column H - Status

        # Notify member
        try:
            await context.bot.send_message(
                chat_id=int(request_info['member_chat_id']),
                text=f"✅ *Project Approved!*\n\n"
                     f"Your suggested project *{request_info['project_name']}* "
                     f"for *{request_info['space_name']}* has been approved!\n\n"
                     "You can now add tasks to this project with /addsched",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying member: {e}")

        await update.message.reply_text(
            f"✅ *Project Added!*\n\n"
            f"You approved *{request_info['member_name']}*'s project:\n\n"
            f"*Project:* {request_info['project_name']}\n"
            f"*Space:* {request_info['space_name']}\n\n"
            f"/showproject - View all projects",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        print(f"Error in approve_project: {e}")
        await update.message.reply_text("⚠️ Error processing approval. Please try again.")


async def reject_project(update: Update, context: CallbackContext) -> None:
    """Reject a suggested project"""
    try:
        command = update.message.text
        # Extract number whether format is /rejectproject123 or /rejectproject_123
        member_chat_id = re.search(r'\d+', command).group()

        pending_sheet = init_google_sheets(PENDING_PROJECTS_SHEET)
        all_requests = pending_sheet.get_all_values()

        request_info = None
        row_index = None

        for i, row in enumerate(all_requests[1:], start=2):  # Skip header
            if (row[0] == str(update.message.chat_id) and
                    row[2] == member_chat_id and
                    row[7].lower() == "pending"):
                request_info = {
                    'member_chat_id': row[2],
                    'member_name': row[3],
                    'space_name': row[5],
                    'project_name': row[6]
                }
                row_index = i
                break

        if not request_info:
            await update.message.reply_text("❌ No pending project suggestion found for this user.")
            return

        # Update status in Pending sheet
        pending_sheet.update_cell(row_index, 8, "Rejected")

        # Notify member
        try:
            await context.bot.send_message(
                chat_id=int(request_info['member_chat_id']),
                text=f"❌ *Project Not Approved*\n\n"
                     f"Your suggested project *{request_info['project_name']}* "
                     f"for *{request_info['space_name']}* was not approved.\n\n"
                     "You can suggest another project with /addproject",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying member: {e}")

        await update.message.reply_text(
            f"❌ Rejected {request_info['member_name']}'s project suggestion: "
            f"*{request_info['project_name']}* for {request_info['space_name']}."
        )

    except Exception as e:
        print(f"Error in reject_project: {e}")
        await update.message.reply_text("⚠️ Error processing rejection. Please try again.")


async def approve_member(update: Update, context: CallbackContext) -> None:
    try:
        command = update.message.text
        # Extract number whether format is /approve123 or /approve_123
        member_chat_id = re.search(r'\d+', command).group()

        # Find the pending request
        pending_sheet = init_pending_joins_sheet()
        all_requests = pending_sheet.get_all_values()

        request_info = None
        row_index = None

        for i, row in enumerate(all_requests[1:], start=2):  # Skip header
            if (row[0] == str(update.message.chat_id) and
                    row[2] == member_chat_id and
                    row[6].lower() == "pending"):
                request_info = {
                    'member_chat_id': row[2],
                    'member_name': row[3],
                    'code_id': row[4],
                    'space_name': row[5],
                    'timestamp': row[1]
                }
                row_index = i
                break

        if not request_info:
            await update.message.reply_text("❌ No pending request found for this user.")
            return

        # Add to Members sheet
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        members_sheet.append_row([
            request_info['member_chat_id'],
            request_info['timestamp'],
            request_info['member_name'],
            request_info['code_id'],
            request_info['space_name']
        ])

        # Update status in Pending sheet
        pending_sheet.update_cell(row_index, 7, "Approved")

        # Notify member
        try:
            await context.bot.send_message(
                chat_id=int(request_info['member_chat_id']),
                text=f"🎉 *Join Request Approved!*\n\n"
                     f"You've been approved to join *{request_info['space_name']}* TeamSpace!\n\n"
                     "You can now:\n"
                     "/showproject - Show all projects.\n"
                     "/addsched - Add schedule.\n"

                ,

                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying member: {e}")

        await update.message.reply_text(
            f"✅ *Thanks!* You have approved {request_info['member_name']}'s request to join {request_info['space_name']}.\n\n"
            "You can now *assign reminders/tasks* to this member.\n\n"
            "/assigsched - Assign Reminders.\n"
            "/addproject - Add project for team.\n"
            "/showproject - Show all projects.\n"
            "/showmember - See all members.",
            parse_mode="Markdown"
        )



    except Exception as e:
        print(f"Error in approve_member: {e}")
        await update.message.reply_text("⚠️ Error processing approval. Please try again.")


async def deny_member(update: Update, context: CallbackContext) -> None:
    try:
        command = update.message.text
        # Extract number whether format is /reject123 or /reject_123
        member_chat_id = re.search(r'\d+', command).group()

        pending_sheet = init_pending_joins_sheet()
        all_requests = pending_sheet.get_all_values()

        request_info = None
        row_index = None

        for i, row in enumerate(all_requests[1:], start=2):  # Skip header
            if (row[0] == str(update.message.chat_id) and
                    row[2] == member_chat_id and
                    row[6].lower() == "pending"):
                request_info = {
                    'member_chat_id': row[2],
                    'member_name': row[3],
                    'space_name': row[5]
                }
                row_index = i
                break

        if not request_info:
            await update.message.reply_text("❌ No pending request found for this user.")
            return

        # Update status in Pending sheet
        pending_sheet.update_cell(row_index, 7, "Denied")

        # Notify member
        try:
            await context.bot.send_message(
                chat_id=int(request_info['member_chat_id']),
                text=f"❌ *Join Request Denied*\n\n"
                     f"Your request to join *{request_info['space_name']}* was not approved.\n\n"
                     "You can request to join another space with /joinspace",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying member: {e}")

        await update.message.reply_text(
            f"❌ Denied {request_info['member_name']}'s request to join {request_info['space_name']}."
        )

    except Exception as e:
        print(f"Error in deny_member: {e}")
        await update.message.reply_text("⚠️ Error processing denial. Please try again.")


async def join_status(update: Update, context: CallbackContext) -> None:
    """Check status of join requests"""
    try:
        pending_sheet = init_google_sheets(PENDING_JOINS_SHEET)
        all_requests = pending_sheet.get_all_values()

        user_requests = []

        for row in all_requests[1:]:  # Skip header
            if row[2] == str(update.message.chat_id):
                user_requests.append({
                    'space_name': row[5],
                    'status': row[6],
                    'timestamp': row[1]
                })

        if not user_requests:
            await update.message.reply_text("ℹ️ You have no pending space join requests.")
            return

        status_message = "⏳ *Your Join Requests*\n\n"
        for req in user_requests:
            status_message += (
                f"• *Space:* {req['space_name']}\n"
                f"  *Status:* {req['status']}\n"
                f"  *Requested:* {req['timestamp']}\n\n"
            )

        await update.message.reply_text(status_message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        print(f"Error in join_status: {e}")
        await update.message.reply_text("⚠️ Error checking your join status. Please try again.")


async def suggestproject_status(update: Update, context: CallbackContext) -> None:
    """Check status of join requests"""
    try:
        pending_sheet = init_google_sheets(PENDING_PROJECTS_SHEET)
        all_requests = pending_sheet.get_all_values()

        user_requests = []

        for row in all_requests[1:]:  # Skip header
            if row[2] == str(update.message.chat_id):
                user_requests.append({
                    'space_name': row[5],
                    'status': row[7],
                    'timestamp': row[1]
                })

        if not user_requests:
            await update.message.reply_text("ℹ️ You have no pending space join requests.")
            return

        status_message = "⏳ *Your suggestion Requests*\n\n"
        for req in user_requests:
            status_message += (
                f"• *Space:* {req['space_name']}\n"
                f"  *Status:* {req['status']}\n"
                f"  *Requested:* {req['timestamp']}\n\n"
            )

        await update.message.reply_text(status_message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        print(f"Error in join_status: {e}")
        await update.message.reply_text("⚠️ Error checking your join status. Please try again.")


async def showspaces_command(update: Update, context: CallbackContext) -> int:
    """Show all created and joined spaces for the user."""
    try:
        context.user_data.clear()
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your spaces...")

        chat_id = str(update.message.chat_id)
        created_spaces = []
        joined_spaces = []

        # Get spaces created by the user (as manager)
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        for row in manager_rows[1:]:
            if len(row) >= 5 and row[0] == chat_id:
                code = row[3]
                space_name = row[4] if len(row) > 4 else "Unnamed Space"
                created_spaces.append((code, space_name))

        # Get spaces joined by the user (as member)
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        member_rows = members_sheet.get_all_values()
        for row in member_rows[1:]:
            if len(row) >= 5 and row[0] == chat_id:
                code = row[3]
                space_name = row[4] if len(row) > 4 else "Unnamed Space"
                # Only add to joined_spaces if not already in created_spaces
                if (code, space_name) not in created_spaces:
                    joined_spaces.append((code, space_name))

        await delete_loading_indicator(update, context)

        if not created_spaces and not joined_spaces:
            await update.message.reply_text(
                "❗ *Oops! Sorry, but you don't have any TeamSpaces yet.*\n\n"
                "💡 You can *create* a *TeamSpace* for your team. Collaborate to create projects, assignments, and set reminders or join any teamspace.\n\n"
                "/joinspace - Join any TeamSpace.\n"
                "/addspace - Create your TeamSpace.\n"
                ,
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        response_message = "🚀 *Your TeamSpaces*\n----------------\n"

        if created_spaces:
            response_message += "\n*Created Spaces:*\n"
            for code, name in sorted(created_spaces, key=lambda x: x[1]):
                response_message += f"   ㆍ `{code}` - {name}\n"
            response_message += "\n*Note:* _Share this space code with your team._\n\n"
            response_message += "/deletespace - Remove a space.\n"
            response_message += "/addproject - Add projects/section to your space.\n"
            response_message += "/addsched - Add schedule.\n"
            response_message += "/addspace - Add more space.\n"

        if joined_spaces:
            if created_spaces:
                response_message += "\n"  # Add spacing
            response_message += "\n*Joined Spaces:*\n"
            for code, name in sorted(joined_spaces, key=lambda x: x[1]):
                response_message += f"   ㆍ `{code}` - {name}\n"
            response_message += "\n*Note:* _Joined spaces cannot be deleted by members. Only the manager can delete them._\n\n"
            response_message += "/addproject - Suggest a project/section.\n"
            response_message += "/addsched - Add schedule.\n"
            response_message += "/unjoinspace - Leave a joined space.\n"

        await update.message.reply_text(
            response_message,
            parse_mode=ParseMode.MARKDOWN
        )

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in showspaces_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your spaces. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def checkprojects_command(update: Update, context: CallbackContext) -> int:
    """Show projects with space names, separated by created and joined projects"""
    loading_message = None
    try:
        # Clear any previous data
        context.user_data.clear()

        # Show loading indicator
        loading_message = await show_loading_indicator(update, context, "🔍 Loading...")

        # Get user's chat ID
        chat_id = str(update.message.chat_id)

        # Initialize data structures
        created_space_codes = set()  # Spaces user created
        joined_space_codes = set()  # Spaces user joined
        space_names = {}  # Map space codes to names
        created_projects = []  # Projects user created
        joined_projects = []  # Projects user joined

        # 1. Get spaces user created (manager)
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        for row in manager_rows[1:]:  # Skip header
            if len(row) > 4 and row[0] == chat_id:  # Column A=chat_id, D=code, E=space_name
                space_code = row[3].upper()
                created_space_codes.add(space_code)
                space_names[space_code] = row[4] if row[4] else "Unnamed Space"

        # 2. Get spaces user joined (member)
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        member_rows = members_sheet.get_all_values()
        for row in member_rows[1:]:  # Skip header
            if len(row) > 4 and row[0] == chat_id:  # Column A=chat_id, D=code, E=space_name
                space_code = row[3].upper()
                if space_code not in created_space_codes:  # Avoid duplicates
                    joined_space_codes.add(space_code)
                    space_names[space_code] = row[4] if row[4] else "Unnamed Space"

        # 3. Get all projects from Projects sheet
        projects_sheet = init_google_sheets("Projects")
        all_projects = projects_sheet.get_all_values()

        for row in all_projects[1:]:  # Skip header
            if len(row) > 4:  # Ensure row has space code and name
                space_code = row[3].upper()
                space_name = space_names.get(space_code, row[4] if len(row) > 4 else "Unnamed Space")
                project_name = row[5] if len(row) > 5 else "Unnamed Project"

                # Format project line with space name in italics
                project_line = f"📂 {project_name} _({space_name})_"

                # Categorize project
                if space_code in created_space_codes:
                    created_projects.append(project_line)
                elif space_code in joined_space_codes:
                    joined_projects.append(project_line)

        # Sort alphabetically
        created_projects.sort()
        joined_projects.sort()

        await delete_loading_indicator(update, context)

        if not created_projects and not joined_projects:
            await update.message.reply_text(
                "ℹ️ You don't have access to any projects yet.\n\n"
                "/addproject - Add own project.\n"
                "/joinspace - Join a TeamSpace."
                ,
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the output
        response = "⏰ *Your Projects*\n----------------\n"

        if created_projects:
            response += "\n*Created Projects:*\n" + "\n".join(f"   {proj}" for proj in created_projects)

        if joined_projects:
            if created_projects:
                response += "\n"  # Add spacing between sections
            response += "\n*Joined Projects:*\n" + "\n".join(f"   {proj}" for proj in joined_projects)

        response += "\n\n/addsched - Add new schedule."
        response += "\n/addproject - Create new projects."
        response += "\n/deleteproject - Delete projects."
        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN
        )

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in checkprojects_command: {str(e)}")
        if loading_message:
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading projects. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def addproject_command(update: Update, context: CallbackContext) -> int:
    """Start the project creation process"""
    try:
        # Clear any previous data
        context.user_data.clear()

        await update.message.reply_text(
            "📝 *Create New Project*\n\n"
            "Please enter a name for your project:\n\n"
            "*Examples:*\n"
            "- Website Redesign\n"
            "- Marketing Campaign\n"
            "- Family Reunion Planning\n\n"
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_PROJECT_NAME

    except Exception as e:
        print(f"Error in addproject_command: {str(e)}")
        await update.message.reply_text(
            "⚠️ Error starting project creation. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_project_name(update: Update, context: CallbackContext) -> int:
    """Process project name input and show available spaces"""
    try:
        project_name = update.message.text.strip()
        if not project_name or len(project_name) > 100:
            await update.message.reply_text(
                "❌ Invalid project name! Please enter a name (max 100 characters).",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_PROJECT_NAME

        # Store project name
        context.user_data['project'] = {'name': project_name}

        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your spaces...")

        chat_id = str(update.message.chat_id)
        created_spaces = []
        joined_spaces = []

        # Get spaces created by the user (as manager)
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        for row in manager_rows[1:]:
            if len(row) >= 5 and row[0] == chat_id:
                code = row[3]
                space_name = row[4] if len(row) > 4 else "Unnamed Space"
                created_spaces.append((code, space_name))

        # Get spaces joined by the user (as member)
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        member_rows = members_sheet.get_all_values()
        for row in member_rows[1:]:
            if len(row) >= 5 and row[0] == chat_id:
                code = row[3]
                space_name = row[4] if len(row) > 4 else "Unnamed Space"
                # Only add to joined_spaces if not already in created_spaces
                if (code, space_name) not in created_spaces:
                    joined_spaces.append((code, space_name))

        await delete_loading_indicator(update, context)

        if not created_spaces and not joined_spaces:
            await update.message.reply_text(
                "❗ *You don't have any spaces yet.*\n\n"
                "You need to create or join a space first:\n\n"
                "/addspace - Create your own space\n"
                "/joinspace - Join an existing space",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the spaces list with sections and commands
        spaces_list = []
        if created_spaces:
            spaces_list.append("*Your Created Spaces:*")
            for code, name in created_spaces:
                spaces_list.append(f"/{code} - {name}")  # Add / prefix to code

        if joined_spaces:
            if created_spaces:
                spaces_list.append("")  # Add empty line between sections
            spaces_list.append("*Joined Spaces:*")
            for code, name in joined_spaces:
                spaces_list.append(f"/{code} - {name}")  # Add / prefix to code

        # Store spaces for selection (without / prefix)
        context.user_data['created_spaces'] = {code: name for code, name in created_spaces}
        context.user_data['joined_spaces'] = {code: name for code, name in joined_spaces}

        await update.message.reply_text(
            "🚀 *Select a Space for Your Project*\n\n"
            + "\n".join(spaces_list) + "\n\n"
                                       "Tap on the space code above to select it (it will auto-send)\n\n"
                                       "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return ADD_PROJECT_SPACE

    except Exception as e:
        print(f"Error in handle_project_name: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your spaces. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_PROJECT_NAME


async def handle_project_space(update: Update, context: CallbackContext) -> int:
    """Process space selection and handle differently for created vs joined spaces"""
    try:
        selected_code_with_slash = update.message.text.strip()
        # Remove leading '/' if present
        selected_code = selected_code_with_slash.lstrip('/').upper()

        created_spaces = context.user_data.get('created_spaces', {})
        joined_spaces = context.user_data.get('joined_spaces', {})

        # Check if code exists in either created or joined spaces
        space_info = None
        is_created_space = False

        if selected_code in created_spaces:
            space_info = {
                'code': selected_code,
                'name': created_spaces[selected_code]
            }
            is_created_space = True
        elif selected_code in joined_spaces:
            space_info = {
                'code': selected_code,
                'name': joined_spaces[selected_code]
            }
            is_created_space = False

        if not space_info:
            await update.message.reply_text(
                "❌ Invalid space code! Please select from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_PROJECT_SPACE

        # Add space info to project data
        context.user_data['project'].update({
            'space_code': space_info['code'],
            'space_name': space_info['name'],
            'is_created_space': is_created_space
        })

        # If it's a created space, create project directly
        if is_created_space:
            return await submit_project(update, context)
        else:
            # For joined spaces, we need to get manager info and send for approval
            loading_msg = await show_loading_indicator(update, context, "🔍 Getting space info...")

            # Get manager info for this space
            worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
            all_values = worksheet.get_all_values()

            manager_info = None
            for row in all_values[1:]:  # Skip header
                if len(row) >= 5 and row[3].upper() == selected_code.upper():  # Column D is space code
                    manager_info = {
                        'chat_id': row[0],
                        'name': row[2],
                        'space_name': row[4]
                    }
                    break

            await delete_loading_indicator(update, context)

            if not manager_info:
                await update.message.reply_text(
                    "❌ Could not find the manager for this space. Please try another space.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return ADD_PROJECT_SPACE

            # Store manager info
            context.user_data['manager_info'] = manager_info

            # Save to pending projects sheet
            loading_msg = await show_loading_indicator(update, context, "⏳ Submitting your project...")

            try:
                worksheet = init_google_sheets(PENDING_PROJECTS_SHEET)
                # Check if headers exist
                if not worksheet.get_values('A1:H1'):
                    worksheet.update('A1:H1', [
                        ['ManagerChatID', 'Timestamp', 'MemberChatID', 'MemberName',
                         'SpaceCode', 'SpaceName', 'ProjectName', 'Status']
                    ])

                # Save to pending projects sheet
                timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
                worksheet.append_row([
                    manager_info['chat_id'],
                    timestamp,
                    update.message.chat_id,
                    update.message.from_user.full_name,
                    selected_code,
                    space_info['name'],
                    context.user_data['project']['name'],
                    "Pending"  # Status
                ])

                # Notify manager
                try:
                    await context.bot.send_message(
                        chat_id=int(manager_info['chat_id']),
                        text=f"💡 *New Project Suggestion*\n\n"
                             f"*From:* {update.message.from_user.full_name}\n"
                             f"*Space:* {space_info['name']} (`{selected_code}`)\n"
                             f"*Project:* {context.user_data['project']['name']}\n\n"
                             "To approve this suggestion:\n"
                             f"/approveproject_{update.message.chat_id}\n\n"
                             "To reject:\n"
                             f"/rejectproject_{update.message.chat_id}",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    print(f"Error notifying manager: {e}")

                await delete_loading_indicator(update, context)

                await update.message.reply_text(
                    "✅ *Project suggestion submitted!*\n\n"
                    f"Your project *{context.user_data['project']['name']}* for *{space_info['name']}* "
                    "has been sent to the manager for approval.\n\n"
                    "You'll receive a notification once reviewed.",
                    parse_mode=ParseMode.MARKDOWN
                )

                return ConversationHandler.END

            except Exception as e:
                await delete_loading_indicator(update, context)
                print(f"Error saving pending project: {e}")
                await update.message.reply_text(
                    "⚠️ Error submitting your project. Please try again.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return ADD_PROJECT_SPACE

    except Exception as e:
        print(f"Error in handle_project_space: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error processing space selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_PROJECT_SPACE


async def submit_project(update: Update, context: CallbackContext) -> int:
    """Save project to Google Sheets (only for created spaces) with hidden project code in Column G"""
    loading_message = None
    try:
        project_data = context.user_data.get('project')
        if not project_data:
            await update.message.reply_text(
                "❌ No project data found. Start over with /addproject",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Only proceed if it's a created space
        if not project_data.get('is_created_space', False):
            await update.message.reply_text(
                "❌ Invalid operation. Projects in joined spaces require approval.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Show loading while saving
        loading_message = await show_loading_indicator(update, context, "⏳ Saving project...")

        # Generate a random 4-character project code (hidden from users)
        project_code = generate_space_code()

        worksheet = init_google_sheets("Projects")
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')

        # Updated row data with project code in Column G (index 6)
        row_data = [
            update.message.chat_id,  # Column A - creator's chat ID
            timestamp,  # Column B - timestamp
            update.message.from_user.full_name,  # Column C - creator name
            project_data['space_code'],  # Column D - space code
            project_data['space_name'],  # Column E - space name
            project_data['name'],  # Column F - project name
            project_code  # Column G - hidden project code
        ]

        worksheet.append_row(row_data)

        await delete_loading_indicator(update, context)

        await update.message.reply_text(
            f"✅ *Project Created Successfully!*\n\n"
            f"*Project Name:* {project_data['name']}\n"
            f"*Space:* {project_data['space_name']}\n\n"
            "/showproject - List of all projects.\n"
            "/deleteproject - Delete your projects.\n"
            "/addsched - Add schedule.\n",
            parse_mode=ParseMode.MARKDOWN
        )

        # Add cleanup here - typically 4-5 messages in the conversation flow
        await cleanup_messages(update, context, num_messages=3)

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_project: {str(e)}")
        if loading_message:
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to save project: {str(e)}\n\n"
            "Please try again with /addproject",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def deleteproject_command(update: Update, context: CallbackContext) -> int:
    """Start the project deletion process"""
    try:
        # Clear any previous data
        context.user_data.clear()

        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your projects...")

        # Get all projects this user has created (as manager)
        worksheet = init_google_sheets("Projects")
        chat_id = str(update.message.chat_id)
        all_values = worksheet.get_all_values()

        # Find all projects created by this user
        user_projects = []
        for row in all_values[1:]:  # Skip header
            if len(row) >= 6 and row[0] == chat_id:  # Column A is creator's chat_id
                project_name = row[5]  # Column F is project name
                space_name = row[4] if len(row) > 4 else "Unnamed Space"  # Column E is space name
                user_projects.append((project_name, space_name))

        await delete_loading_indicator(update, context)

        if not user_projects:
            await update.message.reply_text(
                "ℹ️ You haven't created own projects to delete.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the projects list with numbered commands
        projects_list = "\n".join(
            f"/{i + 1} - {project_name} ({space_name})"
            for i, (project_name, space_name) in enumerate(user_projects)
        )

        # Store projects data with index mapping
        context.user_data['delete_projects'] = {
            str(i + 1): {'name': project_name, 'space': space_name}
            for i, (project_name, space_name) in enumerate(user_projects)
        }

        await update.message.reply_text(
            "🗑 *Delete a Project*\n\n"
            "*Which project would you like to delete?*\n\n"
            f"{projects_list}\n\n"
            "Type /cancel to stop.",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_PROJECT_SELECT

    except Exception as e:
        print(f"Error in deleteproject_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your projects. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_deleteproject_select(update: Update, context: CallbackContext) -> int:
    """Handle selected project for deletion"""
    try:
        command = update.message.text.strip()

        # Check for cancellation first
        if command.lower() == '/cancel':
            await update.message.reply_text(
                "❌ Project deletion cancelled.",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data.clear()  # Clear all conversation data
            return ConversationHandler.END

        if not command.startswith('/'):
            await update.message.reply_text(
                "❌ Please select a project using the numbered commands.",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_PROJECT_SELECT

        # Extract number from command
        selected_num = command[1:]  # Remove the leading '/'
        projects = context.user_data.get('delete_projects', {})

        if selected_num not in projects:
            await update.message.reply_text(
                "❌ Invalid selection. Please choose a number from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_PROJECT_SELECT

        # Get selected project details
        selected_project = projects[selected_num]

        # Store selected project
        context.user_data['delete_selected'] = {
            'project_name': selected_project['name'],
            'space_name': selected_project['space']
        }

        await update.message.reply_text(
            f"⚠️ *Confirm Project Deletion*\n\n"
            f"*Project:* {selected_project['name']}\n"
            f"*Space:* {selected_project['space']}\n\n"
            "This will permanently delete:\n"
            "- The project record\n"
            "- All associated reminders\n\n"
            "*Are you sure?*\n\n"
            "Yes - /submit\n"
            "No - /cancel",
            parse_mode=ParseMode.MARKDOWN
        )

        return DEL_PROJECT_CONFIRM

    except Exception as e:
        print(f"Error in handle_deleteproject_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return DEL_PROJECT_SELECT


async def submit_deleteproject(update: Update, context: CallbackContext) -> int:
    """Delete the selected project and all related reminders"""
    try:
        selected_project = context.user_data.get('delete_selected', {})
        if not selected_project:
            await update.message.reply_text(
                "❌ No project selected. Start over with /deleteproject",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        project_name = selected_project['project_name']
        space_name = selected_project['space_name']

        loading_msg = await show_loading_indicator(update, context, "⏳ Deleting project and related data...")

        # 1. Delete from Projects sheet
        projects_sheet = init_google_sheets("Projects")
        all_projects = projects_sheet.get_all_values()
        projects_deleted = 0

        # Delete rows in reverse order
        for i in range(len(all_projects) - 1, 0, -1):  # Skip header
            if len(all_projects[i]) > 5 and all_projects[i][5] == project_name:
                projects_sheet.delete_rows(i + 1)  # Rows are 1-based
                projects_deleted += 1

        # 2. Delete from Added Reminders sheet
        reminders_sheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        all_reminders = reminders_sheet.get_all_values()
        reminders_deleted = 0

        for i in range(len(all_reminders) - 1, 0, -1):  # Skip header
            if len(all_reminders[i]) > 8 and all_reminders[i][8] == project_name:
                reminders_sheet.delete_rows(i + 1)
                reminders_deleted += 1
        # 3. Clear timestamps in RemindersRoot for deleted reminders
        reminders_root = init_google_sheets("RemindersRoot")
        reminders_root.batch_clear(["M3:M", "AA3:AA"])

        await delete_loading_indicator(update, context)

        await update.message.reply_text(
            f"✅ *Project Deleted Successfully!*\n\n"
            f"*Project:* {project_name}\n"
            f"*Space:* {space_name}\n\n"
            f"*Deleted:*\n"
            f"- {projects_deleted} project records\n"
            f"- {reminders_deleted} associated reminders\n\n"
            f"/showproject - View remaining projects",
            parse_mode=ParseMode.MARKDOWN
        )

        # Cleanup previous messages
        await cleanup_messages(update, context, num_messages=3)

        # Clear conversation data
        context.user_data.clear()

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_deleteproject: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to delete project: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def unjoinspace_command(update: Update, context: CallbackContext) -> int:
    """Start the space unjoin process with command-style selection"""
    try:
        context.user_data.clear()
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your joined spaces...")

        # Get all spaces this member has joined
        worksheet = init_google_sheets(MEMBERS_SHEET)
        chat_id = str(update.message.chat_id)
        all_values = worksheet.get_all_values()

        # Find all unique spaces this member has joined (code + name)
        member_spaces = {}
        for row in all_values[1:]:  # Skip header
            if len(row) >= 5 and row[0] == chat_id:  # Check chat_id in column A
                code = row[3]  # Column D
                space_name = row[4] if len(row) > 4 else 'Unnamed Space'  # Column E
                member_spaces[code] = space_name

        await delete_loading_indicator(update, context)

        if not member_spaces:
            await update.message.reply_text(
                "ℹ️ You haven't joined any spaces yet.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the spaces list with / commands
        spaces_list = "\n".join(
            f"`{code}` - {name}"  # Changed from /{code} to just show the code
            for code, name in member_spaces.items()
        )

        # Store spaces data
        context.user_data['unjoin_spaces'] = member_spaces

        # And update the prompt message to:
        await update.message.reply_text(
            "⚠️ *Which space would you like to unjoin?*\n\n"
            f"{spaces_list}\n\n"
            "Please enter the *space code* exactly as shown above\n\n"
            "Type /cancel to stop",
            parse_mode=ParseMode.MARKDOWN
        )

        return UNJOIN_SPACE_SELECT

    except Exception as e:
        print(f"Error in unjoinspace_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your spaces. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_unjoin_select(update: Update, context: CallbackContext) -> int:
    """Handle selected space for unjoining using command-style selection"""
    try:
        selected_code_with_slash = update.message.text.strip()
        # Remove leading '/' if present
        selected_code = selected_code_with_slash.lstrip('/').upper()
        spaces = context.user_data.get('unjoin_spaces', {})

        if selected_code not in spaces:
            await update.message.reply_text(
                "❌ Invalid code selection. Please choose from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return UNJOIN_SPACE_SELECT

        # Get manager info for this space
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        manager_info = None

        for row in manager_rows[1:]:  # Skip header
            if len(row) >= 5 and row[3].upper() == selected_code.upper():  # Column D is space code
                manager_info = {
                    'chat_id': row[0],
                    'name': row[2],
                    'space_name': row[4]
                }
                break

        context.user_data['unjoin_selected'] = selected_code
        context.user_data['unjoin_space_name'] = spaces[selected_code]
        context.user_data['manager_info'] = manager_info  # Store manager info for notification

        await update.message.reply_text(
            f"⚠️ *Confirm Unjoining Space*\n\n"
            f"*Space Code:* `{selected_code}`\n"
            f"*Space Name:* {spaces[selected_code]}\n\n"
            "*This will remove you from this space and its reminders.*\n\n"
            "Are you sure?\n\n"
            "✅ Yes - /submit\n"
            "❌ No - /cancel",
            parse_mode=ParseMode.MARKDOWN
        )

        return UNJOIN_SPACE_CONFIRM

    except Exception as e:
        print(f"Error in handle_unjoin_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return UNJOIN_SPACE_SELECT


async def submit_unjoin(update: Update, context: CallbackContext) -> int:
    """Delete the member's space record from sheet and related reminders"""
    try:
        selected_code = context.user_data.get('unjoin_selected')
        space_name = context.user_data.get('unjoin_space_name')
        manager_info = context.user_data.get('manager_info')

        if not selected_code:
            await update.message.reply_text(
                "❌ No space selected. Start over with /unjoinspace",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Processing unjoin request...")
        chat_id = str(update.message.chat_id)
        member_name = update.message.from_user.full_name

        # 1. Delete from Members sheet
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        all_members = members_sheet.get_all_values()
        rows_to_delete = []

        for i in range(len(all_members) - 1, 0, -1):  # Skip header, search bottom-up
            row = all_members[i]
            if len(row) > 3 and row[0] == chat_id and row[3].upper() == selected_code.upper():
                rows_to_delete.append(i + 1)  # Rows are 1-indexed

        if not rows_to_delete:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                f"ℹ️ You're not a member of space `{selected_code}`",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Delete the row(s)
        for row_num in sorted(rows_to_delete, reverse=True):
            members_sheet.delete_rows(row_num)

        # 2. Delete from Added Reminders sheet
        reminders_sheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        all_reminders = reminders_sheet.get_all_values()
        reminder_rows_to_delete = []

        for i in range(len(all_reminders) - 1, 0, -1):  # Skip header, search bottom-up
            row = all_reminders[i]
            if len(row) > 0 and row[0] == chat_id:  # Column A is chat_id
                reminder_rows_to_delete.append(i + 1)

        # Delete rows in reverse order
        for row_num in sorted(reminder_rows_to_delete, reverse=True):
            reminders_sheet.delete_rows(row_num)

        # 3. Clear timestamps in RemindersRoot
        reminders_root = init_google_sheets("RemindersRoot")
        reminders_root.batch_clear(["M3:M", "AA3:AA"])

        # Notify manager if exists
        if manager_info:
            try:
                await context.bot.send_message(
                    chat_id=int(manager_info['chat_id']),
                    text=f"🚪 *Member Left Space*\n\n"
                         f"*Member:* {member_name}\n"
                         f"*Space:* {space_name} (`{selected_code}`)\n\n"
                         f"/members - View remaining members",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                print(f"Error notifying manager: {e}")

        await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"✅ *You have successfully unjoined the space!*\n\n"
            f"*Space:* {space_name}\n"
            f"*Code:* `{selected_code}`\n\n"
            "All related schedules have been removed.",
            parse_mode=ParseMode.MARKDOWN
        )

        # Clean up previous messages
        await cleanup_messages(update, context, num_messages=3)

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_unjoin: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to unjoin space: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def revokecode_command(update: Update, context: CallbackContext) -> int:
    """Start the code revocation process"""
    try:
        # Clear any previous revoke data first
        context.user_data.pop('revoke_codes', None)
        context.user_data.pop('revoke_selected', None)
        context.user_data.pop('revoke_space_name', None)

        # Clear any previous revoke data first
        context.user_data.clear()

        loading_msg = await show_loading_indicator(update, context, "🔍 Loading...")

        # Get all codes for this manager
        worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        chat_id = str(update.message.chat_id)

        # Get all values from the sheet
        all_values = worksheet.get_all_values()

        # Find all rows where column A (index 0) matches the chat_id
        manager_codes = []
        for row in all_values[1:]:  # Skip header row if exists
            if len(row) >= 5 and row[0] == chat_id:  # Check if row has enough columns and chat_id matches
                code = row[3]  # Column D (index 3)
                space_name = row[4] if len(row) > 4 else ''  # Column E (index 4)
                manager_codes.append((code, space_name))

        await delete_loading_indicator(update, context)

        if not manager_codes:
            await update.message.reply_text(
                "❗️️ *You haven't created any TeamSpace yet.*",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Format the codes list with monospace formatting
        codes_list = "\n".join(
            f"ㆍ /{code}: {name}"  # Add '/' prefix here for easy copy/paste
            for code, name in manager_codes
        )

        # Store codes without / prefix for easy matching
        context.user_data['revoke_codes'] = {
            code: name for code, name in manager_codes
        }

        await update.message.reply_text(
            "🗑 *Which TeamSpace would you like to delete?*\n\n"
            f"{codes_list}\n\n"
            "*Instruction:* _Tap the code you want to delete (it will auto-send)._\n\n"
            "/cancel - Cancel deletion.",
            parse_mode=ParseMode.MARKDOWN
        )

        return REVOKE_CODE_SELECT

    except Exception as e:
        print(f"Error in revokecode_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error loading your codes. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_revoke_select(update: Update, context: CallbackContext) -> int:
    """Handle selected code for revocation"""
    try:
        selected_code_with_slash = update.message.text.strip()
        # Remove leading '/' if present
        selected_code = selected_code_with_slash.lstrip('/').upper()

        codes = context.user_data.get('revoke_codes', {})

        # Check if the code exists
        if selected_code not in codes:
            await update.message.reply_text(
                "❌ Invalid code selection. Please choose from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return REVOKE_CODE_SELECT

        context.user_data['revoke_selected'] = selected_code
        context.user_data['revoke_space_name'] = codes[selected_code]

        await update.message.reply_text(
            f"⚠️ *Confirm Deletion*\n\n"

            "—————————————————\n"
            f"ㆍ *Space Code:* `{selected_code}`\n"
            f"ㆍ *Space Name:* {codes[selected_code]}\n"
            "—————————————————\n\n"
            f"*Note:* _This will permanently delete this team code and all associated data._\n\n"
            "*Are you sure?*\n"
            "/submit - *Yes*\n"
            "/cancel - *Cancel*\n"

            ,
            parse_mode=ParseMode.MARKDOWN
        )

        return REVOKE_CODE_CONFIRM

    except Exception as e:
        print(f"Error in handle_revoke_select: {str(e)}")
        await update.message.reply_text(
            "❌ Error processing your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return REVOKE_CODE_SELECT


async def submit_revoke(update: Update, context: CallbackContext) -> int:
    """Delete the selected space and all related data"""
    try:
        selected_code = context.user_data.get('revoke_selected')
        if not selected_code:
            await update.message.reply_text(
                "❌ No space selected. Start over with /deletespace",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Deleting space and all related data...")

        # 1. Delete from Proj Managers (last)
        proj_managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        proj_managers_data = proj_managers_sheet.get_all_values()
        proj_managers_rows_to_delete = []

        # 2. Delete from Projects
        projects_sheet = init_google_sheets("Projects")
        projects_data = projects_sheet.get_all_values()
        projects_rows_to_delete = []
        related_projects = set()

        # 3. Delete from Members
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        members_data = members_sheet.get_all_values()
        members_rows_to_delete = []

        # 4. Delete from Added Reminders
        reminders_sheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        reminders_data = reminders_sheet.get_all_values()
        reminders_rows_to_delete = []

        # 5. Delete from RemindersRoot (timestamps)
        reminders_root_sheet = init_google_sheets("RemindersRoot")
        reminders_root_data = reminders_root_sheet.get_all_values()
        reminders_root_rows_to_delete = []

        # Find all rows to delete (in reverse order)
        # Proj Managers (delete last)
        for i in range(len(proj_managers_data) - 1, 0, -1):  # Skip header
            if len(proj_managers_data[i]) > 3 and proj_managers_data[i][3].upper() == selected_code.upper():
                proj_managers_rows_to_delete.append(i + 1)  # Rows are 1-indexed

        # Projects
        for i in range(len(projects_data) - 1, 0, -1):
            if len(projects_data[i]) > 3 and projects_data[i][3].upper() == selected_code.upper():
                projects_rows_to_delete.append(i + 1)
                if len(projects_data[i]) > 5:  # Column F - project name
                    related_projects.add(projects_data[i][5])

        # Members
        for i in range(len(members_data) - 1, 0, -1):
            if len(members_data[i]) > 3 and members_data[i][3].upper() == selected_code.upper():
                members_rows_to_delete.append(i + 1)

        # Added Reminders (by project name)
        for i in range(len(reminders_data) - 1, 0, -1):
            if len(reminders_data[i]) > 8 and reminders_data[i][8] in related_projects:  # Column I - project
                reminders_rows_to_delete.append(i + 1)

        # RemindersRoot (by timestamp - column M)
        # Note: This assumes timestamps match exactly. You may need to adjust comparison logic.
        reminders_root_sheet = init_google_sheets("RemindersRoot")
        root_data = reminders_root_sheet.get_all_values()

        for i in range(len(root_data) - 1, 0, -1):
            if len(root_data[i]) > 12:  # Column M
                # Compare timestamp with those being deleted
                for row_num in projects_rows_to_delete + members_rows_to_delete + reminders_rows_to_delete:
                    original_row = row_num - 1  # Convert to 0-index
                    original_sheet = None
                    if row_num in projects_rows_to_delete:
                        original_sheet = projects_data
                    elif row_num in members_rows_to_delete:
                        original_sheet = members_data
                    elif row_num in reminders_rows_to_delete:
                        original_sheet = reminders_data

                    if original_sheet and len(original_sheet[original_row]) > 1:
                        if root_data[i][12] == original_sheet[original_row][1]:  # Compare timestamps
                            # Clear both columns M and AA
                            reminders_root_sheet.update_cell(i + 1, 13, "")  # Column M
                            reminders_root_sheet.update_cell(i + 1, 27, "")  # Column AA
                            break

        # Execute deletions (in reverse order)
        # 1. Added Reminders
        for row in sorted(reminders_rows_to_delete, reverse=True):
            reminders_sheet.delete_rows(row)

        # 2. Projects
        for row in sorted(projects_rows_to_delete, reverse=True):
            projects_sheet.delete_rows(row)

        # 3. Members
        for row in sorted(members_rows_to_delete, reverse=True):
            members_sheet.delete_rows(row)

        # 4. RemindersRoot (by timestamp - columns M and AA)
        for row in sorted(reminders_root_rows_to_delete, reverse=True):
            # Clear both columns M and AA without deleting row
            reminders_root_sheet.update_cell(row, 13, "")  # Column M is 13th column (1-based)
            reminders_root_sheet.update_cell(row, 27, "")  # Column AA is 27th column (1-based)

        # 5. Proj Managers (last)
        for row in sorted(proj_managers_rows_to_delete, reverse=True):
            proj_managers_sheet.delete_rows(row)

        message = (
            f"✅ *Space successfully deleted!*\n\n"
            f"Code: `{selected_code}`\n"
            f"Deleted:\n"
            f"- {len(proj_managers_rows_to_delete)} manager records\n"
            f"- {len(projects_rows_to_delete)} projects\n"
            f"- {len(members_rows_to_delete)} member records\n"
            f"- {len(reminders_rows_to_delete)} reminders\n"
            f"- {len(reminders_root_rows_to_delete)} timestamp records\n\n"
            f"/showspace - View remaining spaces"
        )

        await delete_loading_indicator(update, context)
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        await cleanup_messages(update, context, num_messages=3)  # Clean up messages after successful deletion

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_revoke: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to delete space: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the current operation with a hard reset"""
    try:
        # Delete any previous messages from the bot
        if 'last_msg' in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data['last_msg']
                )
            except Exception:
                pass
            del context.user_data['last_msg']

        # Clear all data
        context.user_data.clear()

        # Send clean cancellation message
        msg = await update.message.reply_text(
            "✅ Current Operation cancelled.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.MARKDOWN
        )

        # Store this message ID so we can clean it up later if needed
        context.user_data['last_msg'] = msg.message_id

        return ConversationHandler.END
    except Exception as e:
        print(f"Error in cancel: {e}")
        return ConversationHandler.END


async def addspace_command(update: Update, context: CallbackContext) -> int:
    """Start the space creation process with auto-generated code and simplified flow."""
    try:

        # Generate a unique code automatically
        code = generate_space_code()
        context.user_data['registration'] = {
            'code_id': code,
            'type': 'manager'
        }

        await delete_loading_indicator(update, context)

        await update.message.reply_text(
            "🚀 *Create New TeamSpace*\n\n"
            f"*Please enter a name for your TeamSpace:*\n\n"
            "*Examples:*\n"
            "ㆍ _Family Group_\n"
            "ㆍ _Project Team_\n"
            "ㆍ _Sports Club_\n\n"
            "/cancel - _Cancel TeamSpace creation._",
            parse_mode=ParseMode.MARKDOWN
        )
        return REGISTER_MANAGER_NAME_INPUT  # Skip straight to name input

    except Exception as e:
        print(f"Error in addspace_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Error starting space creation. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_manager_name_input(update: Update, context: CallbackContext) -> int:
    """Process space name input and immediately create the space."""
    try:
        space_name = update.message.text.strip()
        if not space_name or len(space_name) > 50:
            await update.message.reply_text(
                "❌ *Invalid TeamSpace name!*\n\n"
                "Please enter a name (max 50 characters)\n\n"
                "Try again or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return REGISTER_MANAGER_NAME_INPUT

        # Get the auto-generated code from context
        registration_data = context.user_data.get('registration', {})
        code_id = registration_data.get('code_id', '')

        if not code_id:
            # Fallback in case code wasn't generated (shouldn't happen with new flow)
            code_id = generate_space_code()
            context.user_data['registration']['code_id'] = code_id

        # Store complete registration data
        context.user_data['registration'].update({
            'name': update.message.from_user.full_name,
            'chat_id': update.message.chat_id,
            'space_name': space_name
        })

        # Directly save to Google Sheets (no confirmation step)
        loading_msg = await show_loading_indicator(update, context, "⏳ Creating your TeamSpace...")

        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
        worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        row_data = [
            context.user_data['registration']['chat_id'],
            timestamp,
            context.user_data['registration']['name'],
            context.user_data['registration']['code_id'],
            context.user_data['registration']['space_name']
        ]
        worksheet.append_row(row_data)

        await delete_loading_indicator(update, context)

        await update.message.reply_text(
            f"✅ *Congratulations! You have created your TeamSpace!*\n\n"
            "—————————————————\n"
            f"ㆍ *TeamSpace Code:* `{code_id}`\n"
            f"ㆍ *TeamSpace Name:* {space_name}\n"
            "—————————————————\n\n"
            f"*Note:* _You can now share your TeamSpace code_ (`{code_id}`) _with your team._\n\n"
            "/addproject - Add Projects.\n"
            "/showspace - Show your TeamSpaces.\n"
            "/deletespace - Delete your TeamSpaces.\n",
            parse_mode=ParseMode.MARKDOWN
        )
        await cleanup_messages(update, context, num_messages=0)  # Clean up messages after successful creation

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in handle_manager_name_input (addspace flow): {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "❌ Error creating your TeamSpace. Please try again or /cancel"
        )
        return REGISTER_MANAGER_NAME_INPUT


async def joinspace_command(update: Update, context: CallbackContext) -> int:
    """Start the space joining process (formerly member registration)"""
    try:
        await update.message.reply_text(
            "💪 *Join a Space*\n\n"
            "Please enter the 4 uppercase letters and numbers *Space Code* provided by your manager.\n\n"
            "/cancel - Cancel joining.\n"
            "/guidelines - Read the guidelines.\n"

            ,
            parse_mode=ParseMode.MARKDOWN
        )
        return REGISTER_MEMBER_INPUT

    except Exception as e:
        print(f"Error in joinspace_command: {str(e)}")
        await update.message.reply_text(
            "⚠️ Error starting space joining. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_member_input(update: Update, context: CallbackContext) -> int:
    """Process member's code input with validation against existing manager codes"""
    try:
        code_id = update.message.text.strip().upper()

        # Basic validation for 4-character code
        if not await validate_code_id(code_id):
            await update.message.reply_text(
                "❌ *Invalid code format!*\n\n"
                "Space codes are 4 uppercase letters and numbers.\n\n"
                "_Example:_ ABCD\n\n"
                "Please check with your manager and try again or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return REGISTER_MEMBER_INPUT

        # Show loading while checking code
        loading_msg = await show_loading_indicator(update, context, "🔍 Checking...")

        # Check if code exists in Proj Managers sheet and get space details
        try:
            worksheet = init_google_sheets(PROJ_MANAGERS_SHEET)
            all_values = worksheet.get_all_values()

            space_info = None
            for row in all_values[1:]:  # Skip header
                if len(row) > 3 and row[3].upper() == code_id:
                    space_info = {
                        'manager_chat_id': row[0],
                        'space_name': row[4] if len(row) > 4 else "Unnamed Space",
                        'manager_name': row[2]
                    }
                    break

            await delete_loading_indicator(update, context)

            if not space_info:
                await update.message.reply_text(
                    "❌ *Invalid space code!*\n\n"
                    f"The code `{code_id}` doesn't match any space.\n\n"
                    "Please:\n"
                    "1. Double-check the code with the manager\n"
                    "2. Make sure you entered it correctly\n"
                    "3. Try again or /cancel",
                    parse_mode=ParseMode.MARKDOWN
                )
                return REGISTER_MEMBER_INPUT

        except Exception as e:
            await delete_loading_indicator(update, context)
            print(f"Error checking space code: {str(e)}")
            await update.message.reply_text(
                "⚠️ *System Error*\n\n"
                "Couldn't verify space code. Please try again later or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return REGISTER_MEMBER_INPUT

        # Store in context
        context.user_data['registration'] = {
            'type': 'member',
            'code_id': code_id,
            'name': update.message.from_user.full_name,
            'chat_id': update.message.chat_id,
            'space_name': space_info['space_name'],
            'manager_chat_id': space_info['manager_chat_id'],
            'manager_name': space_info['manager_name'],
            'pending_approval': True
        }

        # Initialize pending joins sheet
        pending_sheet = init_pending_joins_sheet()
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')

        # Save to pending joins sheet
        pending_sheet.append_row([
            space_info['manager_chat_id'],
            timestamp,
            update.message.chat_id,
            update.message.from_user.full_name,
            code_id,
            space_info['space_name'],
            "Pending"  # Status
        ])

        # Notify manager
        try:
            # In handle_member_input function, change this part:
            await context.bot.send_message(
                chat_id=int(space_info['manager_chat_id']),
                text=f"🔔 *New Join Request*\n\n"
                     f"*User:* {update.message.from_user.full_name}\n"
                     f"*Space:* {space_info['space_name']} (`{code_id}`)\n\n"
                     "To approve this request:\n"
                     f"/approve_{update.message.chat_id}\n\n"  # Added underscore here
                     "To reject:\n"
                     f"/reject_{update.message.chat_id}",  # Added underscore here
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error notifying manager: {e}")

        await update.message.reply_text(
            "⏳ *Join request sent!*\n\n"
            f"Your request to join *{space_info['space_name']}* has been sent to the SpaceCreator.\n\n"
            "*You'll receive a notification once approved.*\n\n"
            "/joinstatus - Check your status.",
            parse_mode=ParseMode.MARKDOWN
        )

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in handle_member_input: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "❌ Error processing your code. Please try again or /cancel"
        )
        return REGISTER_MEMBER_INPUT


async def addrem_command(update: Update, context: CallbackContext) -> int:
    """Start the reminder creation process"""
    try:
        # Clear any previous data
        context.user_data.clear()

        loading_msg = await show_loading_indicator(update, context, "🔍 Checking your spaces...")

        # Check if user has any spaces (created or joined)
        chat_id = str(update.message.chat_id)

        # Check created spaces
        managers_sheet = init_google_sheets(PROJ_MANAGERS_SHEET)
        manager_rows = managers_sheet.get_all_values()
        has_created_spaces = any(row[0] == chat_id for row in manager_rows[1:])

        # Check joined spaces
        members_sheet = init_google_sheets(MEMBERS_SHEET)
        member_rows = members_sheet.get_all_values()
        has_joined_spaces = any(row[0] == chat_id for row in member_rows[1:])

        await delete_loading_indicator(update, context)

        if not has_created_spaces and not has_joined_spaces:
            await update.message.reply_text(
                "❌ *You need to join or create a space first!*\n\n"
                "You can't create schedules until you:\n"
                "1. Create your own space with /addspace\n"
                "2. Or join an existing space with /joinspace\n\n"
                "After joining a space, you'll be able to create schedules.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "🙏 *Please enter your schedule in this format:*\n\n"
            "`Date, Time, Recurrence, Reminder Text`\n\n"
            "*Example:*\n"
            "`6/21/25, 8:00 PM, O, Meeting`\n\n"
            "*Note:*\n"
            "- Recurrence: Once/Daily/Weekly/Monthly/Yearly (or first letter)\n\n"
            "Type /cancel to stop.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_REMINDER_INPUT

    except Exception as e:
        print(f"Error in addsched_command: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "⚠️ Sorry, I couldn't start the schedule process. Please try again."
        )
        return ConversationHandler.END


async def handle_addrem_input(update: Update, context: CallbackContext) -> int:
    """Process the reminder input with validation"""
    try:
        text = update.message.text.strip()
        if text.startswith('/'):
            await update.message.reply_text("❌ Please enter your reminder first.")
            return ADD_REMINDER_INPUT

        loading_msg = await show_loading_indicator(update, context, "🔍 Processing...")

        parts = [p.strip() for p in re.split(r'[,;]', text)]
        if len(parts) < 4:
            await delete_loading_indicator(update, context)
            raise ValueError("Not enough parts (need date, time, recurrence, and text)")

        # Parse date
        date_part = parts[0]
        month, day, year = parse_flexible_date(date_part)
        formatted_date = f"{month}/{day}/{year}"
        reminder_date = datetime.date(year, month, day)
        date_display = reminder_date.strftime("%B %d, %Y")
        weekday = reminder_date.strftime("%A")

        # Parse time
        time_part = parts[1]
        formatted_time = parse_flexible_time(time_part)

        # Parse recurrence
        recurrence_part = parts[2].strip().upper()[0]
        recurrence_map = {
            'O': 'Once',
            'D': 'Daily',
            'W': 'Weekly',
            'M': 'Monthly',
            'Y': 'Yearly'
        }
        if recurrence_part not in recurrence_map:
            await delete_loading_indicator(update, context)
            raise ValueError("Invalid recurrence. Use: Once/Daily/Weekly/Monthly/Yearly")
        recurrence_word = recurrence_map[recurrence_part]

        # Get reminder text
        reminder_text = ','.join(parts[3:]).strip()
        if not reminder_text:
            await delete_loading_indicator(update, context)
            raise ValueError("Reminder text cannot be empty")

        # Generate ID
        reminder_id = await get_next_reminder_id(update.message.chat_id)

        # Store data
        context.user_data['reminder'] = {
            'date': formatted_date,
            'date_display': date_display,
            'time': formatted_time,
            'recurrence': recurrence_part,
            'recurrence_word': recurrence_word,
            'text': reminder_text,
            'id': reminder_id,
            'weekday': weekday,
            'day': day,
            'month': month
        }

        await delete_loading_indicator(update, context)

        # Get user's projects - BOTH AS MANAGER AND MEMBER
        loading_msg = await show_loading_indicator(update, context, "🔍 Loading your projects...")
        try:
            chat_id = str(update.message.chat_id)
            created_projects = set()
            joined_projects = set()

            # 1. Get projects user created (as manager)
            projects_sheet = init_google_sheets("Projects")
            project_rows = projects_sheet.get_all_values()
            for row in project_rows[1:]:  # Skip header
                if len(row) > 0 and row[0] == chat_id:  # Column A is creator's chat_id
                    project_name = row[5] if len(row) > 5 else "Unnamed Project"
                    created_projects.add(project_name)

            # 2. Get projects user joined (as member)
            members_sheet = init_google_sheets(MEMBERS_SHEET)
            member_rows = members_sheet.get_all_values()

            # Get space codes user is member of
            member_space_codes = set()
            for row in member_rows[1:]:  # Skip header
                if len(row) > 3 and row[0] == chat_id:  # Column A is chat_id
                    member_space_codes.add(row[3].upper())  # Column D is space code

            # Find projects in these spaces that user didn't create
            for row in project_rows[1:]:  # Skip header
                if len(row) > 3 and row[3].upper() in member_space_codes:  # Column D is space code
                    project_name = row[5] if len(row) > 5 else "Unnamed Project"
                    if project_name not in created_projects:  # Avoid duplicates
                        joined_projects.add(project_name)

            # Convert to sorted lists
            created_projects = sorted(created_projects)
            joined_projects = sorted(joined_projects)

            if not created_projects and not joined_projects:
                await delete_loading_indicator(update, context)
                await update.message.reply_text(
                    "❌ You don't have any projects yet. Please create or join a project first with /addproject.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return ConversationHandler.END

            # Store projects for selection
            context.user_data['created_projects'] = created_projects
            context.user_data['joined_projects'] = joined_projects

            await delete_loading_indicator(update, context)

            # Format project list with sections - REMOVED GENERAL OPTION
            projects_list = []
            if created_projects:
                projects_list.append("*Created Projects:*")
                for i, project in enumerate(created_projects, start=1):
                    projects_list.append(f"/{i} - {project}")

            if joined_projects:
                if created_projects:
                    projects_list.append("")  # Add empty line between sections
                projects_list.append("*Joined Projects:*")
                for i, project in enumerate(joined_projects, start=len(created_projects) + 1):
                    projects_list.append(f"/{i} - {project}")

            # Store projects with their numbers for reference
            all_projects = created_projects + joined_projects
            context.user_data['numbered_projects'] = {
                str(i + 1): project for i, project in enumerate(all_projects)
            }

            await update.message.reply_text(
                "📂 *Select a project for this schedule:*\n\n" +
                "\n".join(projects_list) + "\n\n" +
                "Tap on the number above to select it\n\n" +
                "Type /cancel to stop",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_REMINDER_PROJECT_SELECT

        except Exception as e:
            await delete_loading_indicator(update, context)
            print(f"Error loading projects: {e}")
            await update.message.reply_text(
                "❌ Error loading your projects. You need to have at least one project to create a schedule.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

    except Exception as e:
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        error_msg = str(e).replace("AW/PM", "AM/PM")  # Fix the typo in error message
        await update.message.reply_text(
            f"❌ Error: {error_msg}\n\n"
            "Please use format:\n"
            "`<date>, <time>, <recurrence>, <your text>`\n\n"
            "*Example:* `June 21 2025, 10:00 AM, W, Weekly meeting`\n\n"
            "Try again or /cancel",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_REMINDER_INPUT


async def handle_project_selection(update: Update, context: CallbackContext) -> int:
    """Handle project selection for the reminder"""
    try:
        command = update.message.text.strip()
        if not command.startswith('/'):
            await update.message.reply_text(
                "❌ Please select a project using the numbered commands.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_REMINDER_PROJECT_SELECT

        # Extract number from command
        selected_num = command[1:]  # Remove the leading '/'
        projects = context.user_data.get('numbered_projects', {})

        if selected_num not in projects:
            await update.message.reply_text(
                "❌ Invalid selection. Please choose a number from the list or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return ADD_REMINDER_PROJECT_SELECT

        # Store selected project
        context.user_data['reminder']['project'] = projects[selected_num]
        return await show_reminder_confirmation(update, context)

    except Exception as e:
        print(f"Error in handle_project_selection: {e}")
        await update.message.reply_text(
            "❌ Error processing project selection. Please try again or /cancel",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_REMINDER_PROJECT_SELECT

        # Store selected project
        context.user_data['reminder']['project'] = selected_project
        return await show_reminder_confirmation(update, context)

    except Exception as e:
        print(f"Error in handle_project_selection: {e}")
        await update.message.reply_text(
            "❌ Error processing project selection. Please try again or /cancel",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_REMINDER_PROJECT_SELECT


async def show_reminder_confirmation(update: Update, context: CallbackContext) -> int:
    """Show the final confirmation for the reminder"""
    reminder = context.user_data.get('reminder', {})
    project_name = reminder.get('project', 'General')

    if reminder['recurrence_word'] == 'Once':
        confirmation_message = (
            f"🔍 *Please confirm the reminder details:*\n\n"
            f"*Date:* {reminder['date_display']}\n"
            f"*Time:* {reminder['time']}\n"
            f"*Recurrence:* {reminder['recurrence_word']}\n"
            f"*Reminder:* {reminder['text']}\n"
            f"*Project:* {project_name}\n\n"
            f"*ID#:* _{reminder['id']}_ (remember this)\n\n"
            "Confirm - /submit\n"
            "Cancel - /cancel"
        )
    elif reminder['recurrence_word'] == 'Daily':
        confirmation_message = (
            f"🔍 *Please confirm the reminder details:*\n\n"
            f"*Starts on:* {reminder['date_display']}\n"
            f"*Time:* {reminder['time']}\n"
            f"*Recurrence:* {reminder['recurrence_word']}\n"
            f"*Reminder:* {reminder['text']}\n"
            f"*Project:* {project_name}\n\n"
            f"*ID#:* _{reminder['id']}_ (remember this)\n\n"
            "Confirm - /submit\n"
            "Cancel - /cancel"
        )
    elif reminder['recurrence_word'] == 'Weekly':
        confirmation_message = (
            f"🔍 *Please confirm the reminder details:*\n\n"
            f"*Starts on:* {reminder['date_display']}\n"
            f"*Every:* {reminder['weekday']}\n"
            f"*Time:* {reminder['time']}\n"
            f"*Recurrence:* {reminder['recurrence_word']}\n"
            f"*Reminder:* {reminder['text']}\n"
            f"*Project:* {project_name}\n\n"
            f"*ID#:* _{reminder['id']}_ (remember this)\n\n"
            "Confirm - /submit\n"
            "Cancel - /cancel"
        )
    elif reminder['recurrence_word'] == 'Monthly':
        confirmation_message = (
            f"🔍 *Please confirm the reminder details:*\n\n"
            f"*Starts on:* {reminder['date_display']}\n"
            f"*Every:* {reminder['day']} of the Month\n"
            f"*Time:* {reminder['time']}\n"
            f"*Recurrence:* {reminder['recurrence_word']}\n"
            f"*Reminder:* {reminder['text']}\n"
            f"*Project:* {project_name}\n\n"
            f"*ID#:* _{reminder['id']}_ (remember this)\n\n"
            "Confirm - /submit\n"
            "Cancel - /cancel"
        )
    elif reminder['recurrence_word'] == 'Yearly':
        confirmation_message = (
            f"🔍 *Please confirm the reminder details:*\n\n"
            f"*Starts on:* {reminder['date_display']}\n"
            f"*Every:* {reminder['month']}/{reminder['day']} of the Year\n"
            f"*Time:* {reminder['time']}\n"
            f"*Recurrence:* {reminder['recurrence_word']}\n"
            f"*Reminder:* {reminder['text']}\n"
            f"*Project:* {project_name}\n\n"
            f"*ID#:* _{reminder['id']}_ (remember this)\n\n"
            "Confirm - /submit\n"
            "Cancel - /cancel"
        )

    await update.message.reply_text(
        confirmation_message,
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_REMINDER_CONFIRM


async def submit_addrem(update: Update, context: CallbackContext) -> int:
    """Save the reminder to Google Sheets with hidden project code in Column J"""
    try:

        # Show loading indicator
        loading_message = await update.message.reply_text("🔍 Saving...")

        reminder = context.user_data.get('reminder')
        if not reminder:
            await update.message.reply_text(
                "❌ No reminder data found. Start over with /addsched",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # 1. SILENTLY GET PROJECT CODE (if project exists and isn't "General")
        project_code = ""
        if 'project' in reminder and reminder['project'] != "General":
            try:
                projects_sheet = init_google_sheets("Projects")
                # Find project by name (Column F)
                project_name_cell = projects_sheet.find(reminder['project'])
                if project_name_cell:
                    # Get corresponding code from Column G (hidden code column)
                    project_code = projects_sheet.cell(project_name_cell.row, 7).value
            except Exception as e:
                print(f"Silent project code lookup failed: {e}")  # Log but don't alert user

        # 2. SAVE TO ADDED REMINDERS SHEET
        worksheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')

        row_data = [
            update.message.chat_id,  # A: Creator Chat ID
            timestamp,  # B: Timestamp
            update.message.from_user.full_name,  # C: Creator Name
            reminder['date'],  # D: Date (MM/DD/YYYY)
            reminder['time'],  # E: Time (HH:MM AM/PM)
            reminder['recurrence_word'],  # F: Recurrence Type
            reminder['text'],  # G: Reminder Text
            str(reminder['id']),  # H: Reminder ID
            reminder.get('project', 'General'),  # I: Project Name (visible to user)
            project_code  # J: Hidden Project Code (NEW)
        ]
        worksheet.append_row(row_data)

        # 3. UPDATE ID TRACKER (background task)
        async def update_id_tracker():
            try:
                worksheet = init_reminder_id_tracker()
                cell = worksheet.find(str(update.message.chat_id))
                if cell:
                    worksheet.update_cell(cell.row, 2, reminder['id'])
                else:
                    worksheet.append_row([update.message.chat_id, reminder['id']])
                # Update cache
                if 'reminders' not in id_cache:
                    id_cache['reminders'] = {}
                id_cache['reminders'][update.message.chat_id] = reminder['id']
            except Exception as e:
                print(f"Background ID tracking failed: {e}")

        asyncio.create_task(update_id_tracker())

        # 4. USER RESPONSE (project code NOT shown)
        await update.message.reply_text(
            "✅ *Schedule successfully saved!*\n\n"
            f"*{reminder['text']}* on *{reminder['date']}* at *{reminder['time']}*\n"
            f"Recurrence: *{reminder['recurrence_word']}*\n"
            f"Project: *{reminder.get('project', 'General')}*\n\n"
            "/addsched - Add another schedule.\n"
            "/showsched - View schedules.\n"
            "/deletesched - Delete schedule.\n",
            parse_mode=ParseMode.MARKDOWN
        )

        # Delete loading indicator before showing results
        if loading_message:
            try:
                await loading_message.delete()
            except Exception as e:
                print(f"Error deleting loading message: {e}")

        # Add cleanup here - this will delete previous messages in the conversation
        await cleanup_messages(update, context, num_messages=6)

        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_addrem: {str(e)}")
        await update.message.reply_text(
            f"⚠️ Failed to save reminder: {str(e)}\n\n"
            "Please try again with /addsched",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def showsched_command(update: Update, context: CallbackContext) -> int:
    """Handle check reminders request by looking up in ShowRequestRoot sheet"""
    loading_message = None
    try:
        # Show loading indicator
        loading_message = await update.message.reply_text("🔍 Checking...")

        # Silent cancellation - clear user data in background
        context.user_data.clear()

        chat_id = update.message.chat_id
        worksheet = init_google_sheets("ShowRequestRoot")

        # Find the user's chat ID in column A
        cell = worksheet.find(str(chat_id))

        # Delete loading indicator before showing results
        if loading_message:
            try:
                await loading_message.delete()
            except Exception as e:
                print(f"Error deleting loading message: {e}")

        if cell:
            # Get the message from column J (10th column)
            message = worksheet.cell(cell.row, 10).value
            if message:
                # Check if message contains warning symbol
                if "⚠️" in message:
                    # Add "💼 Opps!" before the warning message from sheet
                    formatted_message = f"💼 *Opps! Sorry po.*\n\n{message}"
                    await update.message.reply_text(
                        formatted_message,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    # Original format for normal messages
                    await update.message.reply_text(
                        f"⏰ *Here are your schedules:*\n"
                        f"{message}\n"
                        f"-------------------------------------\n\n"
                        f"/addsched - Add a reminder\n"  # Changed from /addrem
                        f"/deletesched - Delete a reminder\n\n"  # Changed from /deleterem
                        f"⚠️ *Please double-check important dates and times.*",
                        parse_mode=ParseMode.MARKDOWN
                    )
                return ConversationHandler.END

        # If no record found at all
        await update.message.reply_text(
            "💼 *Opps! Sorry po.*\n\n"
            "⚠️ No reminders found. Add some with /addsched",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    except Exception as e:
        print(f"Error in addsched_command: {str(e)}")
        # Ensure loading message is deleted even if an error occurs
        if loading_message:
            try:
                await loading_message.delete()
            except Exception as e:
                print(f"Error deleting loading message: {e}")

        await update.message.reply_text(
            f"⚠️ Error checking your reminders: {str(e)}\n\n"
            "Please try again later po.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def deletesched_command(update: Update, context: CallbackContext) -> int:
    """Start delete reminder process with formatted display from RequestReview sheet"""
    loading_message = None
    try:
        # Show loading indicator
        loading_message = await update.message.reply_text("⏳ Loading your schedules...")

        # Silent cancellation - clear user data in background
        context.user_data.clear()

        chat_id = update.message.chat_id
        worksheet = init_google_sheets("ShowRequestRoot")

        # Find the user's chat ID in column A
        cell = worksheet.find(str(chat_id))

        # Delete loading indicator before showing results
        if loading_message:
            try:
                await loading_message.delete()
            except Exception as e:
                print(f"Error deleting loading message: {e}")

        if cell:
            # Get the message from column J (10th column)
            message = worksheet.cell(cell.row, 10).value

            if message:
                if "⚠️" in message:
                    formatted_message = f"⏰ *Opps! Sorry po.*\n\n{message}"
                    await update.message.reply_text(
                        formatted_message,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return ConversationHandler.END
                else:
                    delete_prompt = (
                        "🗑 *Which schedule would you like to delete?*\n"
                        f"{message}\n"
                        f"-------------------------------------\n\n"
                        "Please send the *schedule ID#* you want to delete:\n\n"
                        "_Example:_ 1\n\n"
                        "*Note:* _Only single numbers are allowed. Click /cancel to stop._\n\n"
                        ""
                    )
                    await update.message.reply_text(
                        delete_prompt,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return DEL_REMINDER_INPUT

        await update.message.reply_text(
            "💼 *Opps! Sorry po.*\n\n"
            "⚠️ No schedule found. Add some with /addsched",  # Changed from /addrem
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    except Exception as e:
        print(f"Error in deleterem_command: {str(e)}")
        # Ensure loading message is deleted even if an error occurs
        if loading_message:
            try:
                await loading_message.delete()
            except Exception as e:
                print(f"Error deleting loading message: {e}")

        await update.message.reply_text(
            f"⚠️ Error preparing reminder list: {str(e)}\n\n"
            "Please try again later.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def handle_delrem_input(update: Update, context: CallbackContext) -> range:
    """Process delete reminder input (single number only)"""
    try:
        if not update or not update.message:
            return DEL_REMINDER_INPUT

        delete_text = update.message.text.strip()

        if delete_text.startswith('/'):
            await update.message.reply_text("❌ Please enter a reminder number first")
            return DEL_REMINDER_INPUT

        # Validate input contains only one number
        if not delete_text.isdigit():
            await update.message.reply_text(
                "❌ *Invalid format!*\n\n"
                "Please enter a single number only:\n"
                "_Example:_ `1` or `3`\n\n"
                "Try again or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
            return DEL_REMINDER_INPUT

        # Store the single number
        context.user_data['delete_request'] = delete_text

        await update.message.reply_text(
            f"🔍 *Delete Request:*\n\n"
            f"Delete reminder with ID#: *{delete_text}*\n\n"
            "Is this correct?\n"
            "*Yes* - /submit\n"
            "*No* - /cancel\n"
            "*Start Over* - /deleterem",
            parse_mode=ParseMode.MARKDOWN
        )
        return DEL_REMINDER_INPUT

    except Exception as e:
        print(f"Error in handle_delrem_input: {str(e)}")
        if update and update.message:
            await update.message.reply_text(
                "❌ Invalid input. Please try again or /cancel",
                parse_mode=ParseMode.MARKDOWN
            )
        return DEL_REMINDER_INPUT


async def submit_delrem(update: Update, context: CallbackContext) -> int:
    """Directly delete reminder from 'Added Reminders' and clear timestamp in 'RemindersRoot' (M3:M)"""
    try:
        if not update or not update.message:
            return ConversationHandler.END

        delete_request = context.user_data.get('delete_request')
        if not delete_request:
            await update.message.reply_text(
                "❌ No deletion request found. Start over with /deleterem",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        loading_msg = await show_loading_indicator(update, context, "⏳ Deleting schedule...")

        chat_id = str(update.message.chat_id)
        reminder_id = str(delete_request)

        # 1. Delete from Added Reminders (Column A = ChatID, Column H = ReminderID)
        added_reminders_sheet = init_google_sheets(ADDED_REMINDERS_SHEET)
        added_data = added_reminders_sheet.get_all_values()

        rows_to_delete = []

        for i in range(len(added_data) - 1, 0, -1):  # Skip header, search bottom-up
            row = added_data[i]
            if len(row) >= 8 and row[0] == chat_id and row[7] == reminder_id:
                rows_to_delete.append(i + 1)  # Rows are 1-indexed
                break  # Delete only the first match (assuming unique ID per user)

        if not rows_to_delete:
            await delete_loading_indicator(update, context)
            await update.message.reply_text(
                f"❌ No reminder found with ID #{reminder_id}",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END

        # Delete the row(s)
        for row_num in sorted(rows_to_delete, reverse=True):
            added_reminders_sheet.delete_rows(row_num)

        # 2. Clear timestamp in RemindersRoot (Column M and AA)
        reminders_root_sheet = init_google_sheets("RemindersRoot")
        root_data = reminders_root_sheet.get_all_values()

        # Clear all timestamps in Column M (M3:M) and Column AA (AA3:AA)
        for i in range(2, len(root_data)):  # Start from row 3 (M3) since M1 is header
            reminders_root_sheet.update_cell(i + 1, 13, "")  # Column M = index 13 (1-based)
            reminders_root_sheet.update_cell(i + 1, 27, "")  # Column AA = index 27 (1-based)

        await delete_loading_indicator(update, context)
        await update.message.reply_text(
            "✅ *Your schedule has been deleted!*\n\n"
            f"Schedule ID #*{reminder_id}* has been deleted.\n\n"
            "/showsched - View remaining schedules.\n"  # Changed from /showrem
            "/addsched - Add new schedules",  # Changed from /addrem
            parse_mode=ParseMode.MARKDOWN
        )

        # Add cleanup here - this will delete previous messages in the conversation
        await cleanup_messages(update, context, num_messages=2)  # Adjust number as needed

        context.user_data.clear()
        return ConversationHandler.END

    except Exception as e:
        print(f"Error in submit_delrem: {str(e)}")
        if 'loading_msg' in locals():
            await delete_loading_indicator(update, context)
        await update.message.reply_text(
            f"⚠️ Failed to delete reminder: {str(e)}",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END


async def timezone_command(update: Update, context: CallbackContext) -> int:
    pending_notifications.discard(update.message.chat_id)
    await update.message.reply_text(
        "*❗Kung taga Pilipinas po kayo, huwag na po itong pansinin po!* Click /cancel.\n\n"
        "Ngunit kung nasa *labas* po kayo ng *Pilipinas*, *please  enter your current date and time based on your location po. (MM/DD/YYYY, HH:MM AM/PM):*\n\n"
        "_Example:_ `5/26/2025, 6:00 AM`\n",
        parse_mode=ParseMode.MARKDOWN
    )
    return TIMEZONE_INPUT


async def handle_timezone_input(update: Update, context: CallbackContext, format_time=None) -> int:
    if not update.message or not update.message.text:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Please send your date and time as text po.",
            parse_mode=ParseMode.MARKDOWN
        )
        return TIMEZONE_INPUT

    user_input = update.message.text
    try:
        # Split the input into date and time parts
        date_part, time_part = [part.strip() for part in user_input.split(',')]

        # Parse the date
        month, day, year = map(int, re.split(r'[/-]', date_part))
        formatted_date = f"{month}/{day}/{year}"

        # Parse the time
        time_str = parse_flexible_time(time_part)  # Use the existing flexible parser
        if not time_str:
            raise ValueError("Invalid time format")

        context.user_data['timezone_data'] = {
            'date': formatted_date,
            'time': time_str
        }

        await update.message.reply_text(
            "🙏 *Please review po:*\n\n"
            f"*Date:* {formatted_date}\n"
            f"*Time:* {time_str}\n\n"
            "Is everything correct?\n\n"
            "Yes - /submit\n"
            "No - /timezone",
            parse_mode=ParseMode.MARKDOWN
        )
        return TIMEZONE_CONFIRM

    except Exception:
        await update.message.reply_text(
            "🙏 *Please check your format po:*\n"
            "It should be: MM/DD/YYYY, HH:MM AM/PM\n\n"
            "_Example:_ 5/26/2025, 6:00 AM\n"
            "_Or:_ 12/1/2025, 10:30 PM",
            parse_mode=ParseMode.MARKDOWN
        )
        return TIMEZONE_INPUT


async def submit_timezone(update: Update, context: CallbackContext) -> int:
    timezone_data = context.user_data.get('timezone_data')
    if not timezone_data:
        await update.message.reply_text("*No data to submit po.*", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    try:
        worksheet = init_google_sheets(TIMEZONE_SHEET)
        timestamp = datetime.datetime.now(PH_TZ).strftime('%Y-%m-%d %H:%M:%S')
        row_data = [
            update.message.chat_id,
            timestamp,
            update.message.from_user.full_name,
            timezone_data['date'],
            timezone_data['time']
        ]
        worksheet.append_row(row_data)

        await update.message.reply_text(
            "*✅ Your timezone info has been recorded!*",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data.clear()
    except Exception as e:
        pending_notifications.add(update.message.chat_id)
        await update.message.reply_text(
            f"*⚠️ Error saving your timezone info:* _{str(e)}_",
            parse_mode=ParseMode.MARKDOWN
        )
    return ConversationHandler.END


async def start(update: Update, context: CallbackContext) -> int:
    user = update.message.from_user
    greeting_name = user.first_name or user.username or "User"

    # Escape any Markdown special characters in the name
    safe_name = greeting_name.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`')

    try:
        await update.message.reply_text(
            f"👋 Hello {greeting_name}! \n\n"
            "Welcome to the Project Management_Beta Bot!\n"
            "Easily manage your office schedules within any TeamSpace project or section — keeping managers, co-managers, and members instantly notified of all important team events."
        )
    except Exception:
        # Fallback to plain text if Markdown fails
        await update.message.reply_text(
            f"👋 Hello {greeting_name}! \n\n"
            "Welcome to the Project Management_Beta Bot!\n"
            "Easily manage your office schedules within any TeamSpace project or section — keeping managers, co-managers, and members instantly notified of all important team events."
        )

    await log_registration(update)
    await help_command(update, context)
    return ConversationHandler.END


async def help_command(update: Update, context: CallbackContext) -> int:
    """Improved help command with smart organization and all available commands"""
    help_text = (
        "🤖 *TeamSpace Schedules Bot - Command Guide*\n\n"

        "🚀 *SPACE MANAGEMENT*\n"
        "/addspace - Create a new TeamSpace\n"
        "/deletespace - Delete your TeamSpace\n"
        "/showspace - List your created/joined spaces\n"
        "/joinspace - Join an existing TeamSpace\n"
        "/unjoinspace - Leave a TeamSpace\n\n"

        "📂 *PROJECTS & SECTIONS*\n"
        "/addproject - Create new project/section\n"
        "/deleteproject - Remove a project\n"
        "/showproject - List all your projects\n\n"

        "⏰ *SCHEDULE MANAGEMENT*\n"
        "/addsched - Add new schedule\n"
        "/deletesched - Delete a schedule\n"
        "/showsched - List all your schedules\n"
        "/assignsched - Assign schedule to member (managers)\n"
        "/schedtoday - Today's schedules\n"
        "/schedtomorrow - Tomorrow's schedules\n"
        "/schedthisweek - This week's schedules\n\n"

        "👥 *TEAM MANAGEMENT*\n"
        "/showmember - List all members in your spaces\n"
        "/deletemember - Remove a member from your space\n"
        "/admin - Admin management commands\n"
        "/addadmin - Add space admin\n"
        "/deleteadmin - Remove admin\n"
        "/showadmin - List admins\n\n"

        "🔍 *STATUS CHECKS*\n"
        "/joinstatus - Check join request status\n"
        "/suggestprojectstatus - Check project suggestion status\n\n"

        "⚙️ *SETTINGS & TOOLS*\n"
        "/timezone - Set timezone (if outside PH)\n"
        "/chatid - Get your chat ID\n"
        "/guidelines - Detailed usage guide\n"
        "/cancel - Cancel current operation\n\n"

        "💡 *TIP:* Use these quick access menus:\n"
        "/space - Space commands\n"
        "/project - Project commands\n"
        "/schedule - Schedule commands\n"
        "/member - Member commands\n"
        "/monitoring - View schedules\n"
        "/status - Status commands\n"
        "/settings - Settings commands"
    )

    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    return ConversationHandler.END


async def chatid_command(update: Update, context: CallbackContext) -> int:
    chat_id = update.message.chat_id
    await update.message.reply_text(
        f"✅ *Your chat ID is:* `{chat_id}`",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


async def guidelines_command(update: Update, context: CallbackContext) -> int:
    """Send a beautifully formatted guide about the bot's features"""
    guidelines_text = (
        "📘 *TeamSpace Schedules Bot Guidelines*\n\n"

        "🤔 *What This Bot Can Do For You?*\n"
        "✨ _This bot will automatically remind you of all your important schedules!_ ✨\n\n"

        "🌟 *Unique Features That Make Us Special:*\n"
        "1. *Collaborative Space Management*\n"
        "   - Create _TeamSpaces_ for different groups (*family*, *work*, *projects*)\n"
        "   - Add *members* and assign *admins* for shared management\n"
        "   - Organize schedules under different *projects/sections*\n"
        "   - `/member` - Full member management (add/view/remove)\n\n"

        "2. *Powerful Scheduling System*\n"
        "   - Set *one-time* or *recurring* schedules (daily/weekly/monthly/yearly)\n"
        "   - Managers get *complete visibility* of all schedules\n"
        "   - *Effortlessly* assign tasks to team members\n\n"

        "3. *Smart Access Control*\n"
        "   - *Managers*: Full control over spaces and members\n"
        "   - *Admins*: Can view all schedules (more features coming soon!)\n"
        "   - *All Users*: Both *office-related* (recommended) and personal schedules\n\n"

        "🛠 *How To Get Started:*\n\n"
        "🚀 *For Managers:*\n"
        "1. `/addspace` - Create your TeamSpace\n"
        "2. Share the *auto-generated code* with your team\n"
        "3. Approve join requests when members use `/joinspace`\n"
        "4. `/addproject` - Add projects/sections for better organization\n"
        "5. `/assignsched` - Assign schedules to team members\n"
        "6. `/showmember` - View and manage all space members\n\n"

        "👥 *For Team Members:*\n"
        "1. `/joinspace` - Join spaces (manager approval required)\n"
        "2. `/schedtoday` - View today's schedule\n"
        "3. `/addsched` - Add your office schedules\n\n"

        "🔐 *Admin Privileges Explained:*\n"
        "- `/admin` - Manage admin permissions\n"
        "- *5 Admin Slots* per space (1 manager + 4 co-admins)\n"
        "- *Managers* have full control including `/deletemember`\n"
        "- *Co-Admins* can view all schedules and members\n\n"

        "💡 *Pro Tips for Power Users:*\n"
        "- `/showspace` - List all your spaces\n"
        "- `/showproject` - View all projects\n"
        "- `/member` - Complete member management console\n"
        "- *Tag schedules* to projects/sections for better organization\n\n"

        "❓ *Need Assistance?*\n"
        "- `/help` - Show all commands\n"
        "- `/cancel` - Stop any current operation\n"
        "- _All commands are case-insensitive_"
    )

    await update.message.reply_text(
        guidelines_text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    return ConversationHandler.END


async def error_handler(update: Update, context: CallbackContext) -> None:
    """Log errors and send a message to the user."""
    print(f"Update {update} caused error {context.error}")
    if update and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ An error occurred. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )


# ======================
# SECTION 4: MAIN APPLICATION SETUP
# ======================
def main() -> None:
    try:
        init_pending_joins_sheet()
    except Exception as e:
        print(f"Error initializing sheets: {e}")

    # Get the token from environment variables
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set!")
        return

    application = (
        Application.builder()
        .token(token)  # Add this line to pass the token
        .job_queue(JobQueue())
        .build()
    )

    # Update your handler registration:
    application.add_handler(MessageHandler(
        filters.Regex(r'^/[a-z]$') & filters.ChatType.PRIVATE,  # Lowercase for showmember
        view_member_schedules
    ))

    deletemember_handler = ConversationHandler(
        entry_points=[CommandHandler("deletemember", deletemember_command)],
        states={
            DEL_PROJECT_SELECT: [
                MessageHandler(filters.Regex(r'^/[A-Z0-9]{4}$') | filters.Regex(r'^/[a-z0-9]{4}$'),
                               handle_deletemember_space),
                CommandHandler("cancel", cancel)
            ],
            DEL_PROJECT_CONFIRM: [
                MessageHandler(filters.Regex(r'^/[A-Z]$'),  # Uppercase for deletemember
                               handle_deletemember_select),
                CommandHandler("submit", submit_deletemember),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    # Add admin management handlers
    addadmin_handler = ConversationHandler(
        entry_points=[CommandHandler("addadmin", addadmin_command)],
        states={
            ADD_ADMIN_SPACE_SELECT: [
                MessageHandler(filters.Regex(r'^/[a-zA-Z0-9]{4}$'), handle_admin_space_select),
                CommandHandler("cancel", cancel)
            ],
            ADD_ADMIN_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_input),
                CommandHandler("cancel", cancel)
            ],
            ADD_ADMIN_CONFIRM: [
                CommandHandler("submit", submit_addadmin),  # This was missing
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    deleteadmin_handler = ConversationHandler(
        entry_points=[CommandHandler("deleteadmin", deleteadmin_command)],
        states={
            DEL_ADMIN_SELECT: [
                MessageHandler(filters.Regex(r'^/\d+$'), handle_deleteadmin_select),
                CommandHandler("cancel", cancel)
            ],
            DEL_ADMIN_CONFIRM: [
                CommandHandler("submit", submit_deleteadmin),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    # Modified addspace_handler for simplified flow
    addspace_handler = ConversationHandler(
        entry_points=[CommandHandler("addspace", addspace_command)],
        states={
            REGISTER_MANAGER_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manager_name_input),
                CommandHandler("cancel", cancel)
            ],
            # No REGISTER_MANAGER_CONFIRM state needed for this simplified flow
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    deleteproject_handler = ConversationHandler(
        entry_points=[CommandHandler("deleteproject", deleteproject_command)],
        states={
            DEL_PROJECT_SELECT: [
                MessageHandler(filters.Regex(r'^/[^/]+$'), handle_deleteproject_select),
                CommandHandler("cancel", cancel)
            ],
            DEL_PROJECT_CONFIRM: [
                CommandHandler("submit", submit_deleteproject),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    addproject_handler = ConversationHandler(
        entry_points=[CommandHandler("addproject", addproject_command)],
        states={
            ADD_PROJECT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_project_name),
                CommandHandler("cancel", cancel)
            ],
            ADD_PROJECT_SPACE: [
                # Accept commands starting with '/' followed by 4 alphanumeric characters
                MessageHandler(filters.Regex(r'^/[a-zA-Z0-9]{4}$'), handle_project_space),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    # Add the revoke handler to your main application setup:
    revoke_handler = ConversationHandler(
        entry_points=[CommandHandler("deletespace", revokecode_command)],
        states={
            REVOKE_CODE_SELECT: [
                # Accept commands starting with '/' followed by 4 alphanumeric characters
                MessageHandler(filters.Regex(r'^/[a-zA-Z0-9]{4}$'), handle_revoke_select),
                CommandHandler("cancel", cancel)
            ],
            REVOKE_CODE_CONFIRM: [
                CommandHandler("submit", submit_revoke),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    joinspace_handler = ConversationHandler(
        entry_points=[CommandHandler("joinspace", joinspace_command)],
        states={
            REGISTER_MEMBER_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_member_input),
                CommandHandler("cancel", cancel)
            ],
            # REGISTER_MEMBER_CONFIRM state is no longer needed as approval is pending
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    addrem_handler = ConversationHandler(
        entry_points=[CommandHandler("addsched", addrem_command)],
        states={
            ADD_REMINDER_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_addrem_input),
                CommandHandler("cancel", cancel)
            ],
            ADD_REMINDER_PROJECT_SELECT: [
                MessageHandler(filters.Regex(r'^/\d+$'), handle_project_selection),
                CommandHandler("cancel", cancel)
            ],
            ADD_REMINDER_CONFIRM: [
                CommandHandler("submit", submit_addrem),
                CommandHandler("addrem", addrem_command),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    deletesched_handler = ConversationHandler(
        entry_points=[CommandHandler("deletesched", deletesched_command)],
        states={
            DEL_REMINDER_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delrem_input),
                CommandHandler("submit", submit_delrem),
                CommandHandler("cancel", cancel),
                CommandHandler("deletesched", deletesched_command)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    timezone_handler = ConversationHandler(
        entry_points=[CommandHandler("timezone", timezone_command)],
        states={
            TIMEZONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_timezone_input),
                CommandHandler("cancel", cancel)
            ],
            TIMEZONE_CONFIRM: [
                CommandHandler("submit", submit_timezone),
                CommandHandler("timezone", timezone_command),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    unjoin_handler = ConversationHandler(
        entry_points=[CommandHandler("unjoinspace", unjoinspace_command)],
        states={
            UNJOIN_SPACE_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unjoin_select),
                CommandHandler("cancel", cancel)
            ],
            UNJOIN_SPACE_CONFIRM: [
                CommandHandler("submit", submit_unjoin),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    # Dynamic handlers for approval/denial
    application.add_handler(MessageHandler(
        filters.Regex(r'^/approve_?\d+$'),
        approve_member
    ))
    application.add_handler(MessageHandler(
        filters.Regex(r'^/reject_?\d+$'),
        deny_member
    ))

    suggestproject_handler = ConversationHandler(
        entry_points=[CommandHandler("suggestproject", suggestproject_command)],
        states={
            SUGGEST_SPACE_SELECT: [
                # Accept commands starting with '/' followed by 4 alphanumeric characters
                MessageHandler(filters.Regex(r'^/[a-zA-Z0-9]{4}$'), handle_suggest_space_select),
                CommandHandler("cancel", cancel)
            ],
            SUGGEST_PROJECT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_suggest_project_name),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    application.add_handler(MessageHandler(
        filters.Regex(r'^/approveproject_?\d+$'),
        approve_project
    ))
    application.add_handler(MessageHandler(
        filters.Regex(r'^/rejectproject_?\d+$'),
        reject_project
    ))

    assignrem_handler = ConversationHandler(
        entry_points=[CommandHandler("assignsched", assignrem_command)],  # Changed from "assignrem"
        states={
            ASSIGN_REM_SELECT_MEMBER: [
                MessageHandler(filters.Regex(r'^/assignremto_\d+$'), handle_assignrem_select),
                CommandHandler("cancel", cancel)
            ],
            ASSIGN_REM_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_assignrem_input),
                CommandHandler("cancel", cancel)
            ],
            ASSIGN_REM_PROJECT_SELECT: [
                MessageHandler(filters.Regex(r'^/\d+$'), handle_assignrem_project),
                CommandHandler("cancel", cancel)
            ],
            ASSIGN_REM_CONFIRM: [
                CommandHandler("submit", submit_assignrem),
                CommandHandler("cancel", cancel)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True
    )

    # Add all handlers
    application.add_handler(CommandHandler("showmember", showmember_command))
    application.add_handler(deletemember_handler)
    application.add_handler(unjoin_handler)
    application.add_handler(assignrem_handler)
    application.add_handler(addspace_handler)
    application.add_handler(joinspace_handler)
    application.add_handler(addrem_handler)
    application.add_handler(CommandHandler("showsched", showsched_command))
    application.add_handler(deletesched_handler)
    application.add_handler(timezone_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("chatid", chatid_command))
    application.add_handler(CommandHandler("guidelines", guidelines_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_error_handler(error_handler)
    application.add_handler(revoke_handler)
    application.add_handler(addproject_handler)
    application.add_handler(deleteproject_handler)
    application.add_handler(CommandHandler("showproject", checkprojects_command))
    application.add_handler(CommandHandler("showspace", showspaces_command))
    application.add_handler(CommandHandler("joinstatus", join_status))
    application.add_handler(CommandHandler("suggestprojectstatus", suggestproject_status))
    application.add_handler(suggestproject_handler)
    application.add_handler(CommandHandler("schedtoday", schedtoday_command))
    application.add_handler(CommandHandler("schedtomorrow", schedtomorrow_command))
    application.add_handler(CommandHandler("schedthisweek", schedthisweek_command))
    application.add_handler(CommandHandler("space", space_command))
    application.add_handler(CommandHandler("project", project_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("monitoring", monitoring_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(addadmin_handler)
    application.add_handler(deleteadmin_handler)
    application.add_handler(CommandHandler("showadmin", showadmin_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("member", member_command))

    # Start notification loop
    async def notify_pending(context=None):
        while True:
            if pending_notifications:
                for chat_id in list(pending_notifications):
                    try:
                        await send_message_safe(
                            chat_id,
                            "✅ *The system is now online. Please resubmit your pending operations.*",
                            context
                        )
                        pending_notifications.remove(chat_id)
                    except Exception as e:
                        print(f"Failed to notify {chat_id}: {e}")
                        pending_notifications.discard(chat_id)
            await asyncio.sleep(60)

    application.job_queue.run_once(lambda ctx: asyncio.create_task(notify_pending()), when=0)

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
