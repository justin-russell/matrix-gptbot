import openai
import markdown2
import duckdb
import tiktoken

import asyncio

from nio import (
    AsyncClient,
    AsyncClientConfig,
    WhoamiResponse,
    DevicesResponse,
    Event,
    Response,
    MatrixRoom,
    Api,
    RoomMessagesError,
    MegolmEvent,
    GroupEncryptionError,
    EncryptionError,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse
)
from nio.crypto import Olm

from typing import Optional, List, Dict
from configparser import ConfigParser
from datetime import datetime

import uuid

from .logging import Logger
from migrations import migrate
from callbacks import RESPONSE_CALLBACKS, EVENT_CALLBACKS
from commands import COMMANDS
from .store import DuckDBStore


class GPTBot:
    # Default values
    database: Optional[duckdb.DuckDBPyConnection] = None
    default_room_name: str = "GPTBot"  # Default name of rooms created by the bot
    default_system_message: str = "You are a helpful assistant."
    # Force default system message to be included even if a custom room message is set
    force_system_message: bool = False
    max_tokens: int = 3000  # Maximum number of input tokens
    max_messages: int = 30  # Maximum number of messages to consider as input
    model: str = "gpt-3.5-turbo"  # OpenAI chat model to use
    matrix_client: Optional[AsyncClient] = None
    sync_token: Optional[str] = None
    logger: Optional[Logger] = Logger()
    openai_api_key: Optional[str] = None

    @classmethod
    def from_config(cls, config: ConfigParser):
        """Create a new GPTBot instance from a config file.

        Args:
            config (ConfigParser): ConfigParser instance with the bot's config.

        Returns:
            GPTBot: The new GPTBot instance.
        """

        # Create a new GPTBot instance
        bot = cls()

        # Set the database connection
        bot.database = duckdb.connect(
            config["Database"]["Path"]) if "Database" in config and "Path" in config["Database"] else None

        # Override default values
        if "GPTBot" in config:
            bot.default_room_name = config["GPTBot"].get(
                "DefaultRoomName", bot.default_room_name)
            bot.default_system_message = config["GPTBot"].get(
                "SystemMessage", bot.default_system_message)
            bot.force_system_message = config["GPTBot"].getboolean(
                "ForceSystemMessage", bot.force_system_message)

        bot.max_tokens = config["OpenAI"].getint("MaxTokens", bot.max_tokens)
        bot.max_messages = config["OpenAI"].getint(
            "MaxMessages", bot.max_messages)
        bot.model = config["OpenAI"].get("Model", bot.model)

        bot.openai_api_key = config["OpenAI"]["APIKey"]

        # Set up the Matrix client

        assert "Matrix" in config, "Matrix config not found"

        homeserver = config["Matrix"]["Homeserver"]
        bot.matrix_client = AsyncClient(homeserver)
        bot.matrix_client.access_token = config["Matrix"]["AccessToken"]
        bot.matrix_client.user_id = config["Matrix"].get("UserID")
        bot.matrix_client.device_id = config["Matrix"].get("DeviceID")

        # Return the new GPTBot instance
        return bot

    async def _get_user_id(self) -> str:
        """Get the user ID of the bot from the whoami endpoint.
        Requires an access token to be set up.

        Returns:
            str: The user ID of the bot.
        """

        assert self.matrix_client, "Matrix client not set up"

        user_id = self.matrix_client.user_id

        if not user_id:
            assert self.matrix_client.access_token, "Access token not set up"

            response = await self.matrix_client.whoami()

            if isinstance(response, WhoamiResponse):
                user_id = response.user_id
            else:
                raise Exception(f"Could not get user ID: {response}")

        return user_id

    async def _last_n_messages(self, room: str | MatrixRoom, n: Optional[int]):
        messages = []
        n = n or bot.max_messages
        room_id = room.room_id if isinstance(room, MatrixRoom) else room

        self.logger.log(
            f"Fetching last {2*n} messages from room {room_id} (starting at {self.sync_token})...")

        response = await self.matrix_client.room_messages(
            room_id=room_id,
            start=self.sync_token,
            limit=2*n,
        )

        if isinstance(response, RoomMessagesError):
            raise Exception(
                f"Error fetching messages: {response.message} (status code {response.status_code})", "error")

        for event in response.chunk:
            if len(messages) >= n:
                break
            if isinstance(event, MegolmEvent):
                try:
                    event = await self.matrix_client.decrypt_event(event)
                except (GroupEncryptionError, EncryptionError):
                    self.logger.log(
                        f"Could not decrypt message {event.event_id} in room {room_id}", "error")
                    continue
            if isinstance(event, RoomMessageText):
                if event.body.startswith("!gptbot ignoreolder"):
                    break
                if not event.body.startswith("!"):
                    messages.append(event)

        self.logger.log(f"Found {len(messages)} messages (limit: {n})")

        # Reverse the list so that messages are in chronological order
        return messages[::-1]

    def _truncate(self, messages: list, max_tokens: Optional[int] = None,
                  model: Optional[str] = None, system_message: Optional[str] = None):
        max_tokens = max_tokens or self.max_tokens
        model = model or self.model
        system_message = self.default_system_message if system_message is None else system_message

        encoding = tiktoken.encoding_for_model(model)
        total_tokens = 0

        system_message_tokens = 0 if not system_message else (
            len(encoding.encode(system_message)) + 1)

        if system_message_tokens > max_tokens:
            self.logger.log(
                f"System message is too long to fit within token limit ({system_message_tokens} tokens) - cannot proceed", "error")
            return []

        total_tokens += system_message_tokens

        total_tokens = len(system_message) + 1
        truncated_messages = []

        for message in [messages[0]] + list(reversed(messages[1:])):
            content = message["content"]
            tokens = len(encoding.encode(content)) + 1
            if total_tokens + tokens > max_tokens:
                break
            total_tokens += tokens
            truncated_messages.append(message)

        return [truncated_messages[0]] + list(reversed(truncated_messages[1:]))

    async def _get_device_id(self) -> str:
        """Guess the device ID of the bot.
        Requires an access token to be set up.

        Returns:
            str: The guessed device ID.
        """

        assert self.matrix_client, "Matrix client not set up"

        device_id = self.matrix_client.device_id

        if not device_id:
            assert self.matrix_client.access_token, "Access token not set up"

            devices = await self.matrix_client.devices()

            if isinstance(devices, DevicesResponse):
                device_id = devices.devices[0].id

        return device_id

    async def process_command(self, room: MatrixRoom, event: RoomMessageText):
        self.logger.log(
            f"Received command {event.body} from {event.sender} in room {room.room_id}")
        command = event.body.split()[1] if event.body.split()[1:] else None

        await COMMANDS.get(command, COMMANDS[None])(room, event, self)

    async def event_callback(self,room: MatrixRoom, event: Event):
        self.logger.log("Received event: " + str(event), "debug")
        for eventtype, callback in EVENT_CALLBACKS.items():
            if isinstance(event, eventtype):
                await callback(room, event, self)

    async def response_callback(self, response: Response):
        for response_type, callback in RESPONSE_CALLBACKS.items():
            if isinstance(response, response_type):
                await callback(response, self)

    async def accept_pending_invites(self):
        """Accept all pending invites."""

        assert self.matrix_client, "Matrix client not set up"

        invites = self.matrix_client.invited_rooms

        for invite in invites.keys():
            await self.matrix_client.join(invite)

    async def send_message(self, room: MatrixRoom, message: str, notice: bool = False):
        markdowner = markdown2.Markdown(extras=["fenced-code-blocks"])
        formatted_body = markdowner.convert(message)

        msgtype = "m.notice" if notice else "m.text"

        msgcontent = {"msgtype": msgtype, "body": message,
                      "format": "org.matrix.custom.html", "formatted_body": formatted_body}

        content = None

        if self.matrix_client.olm and room.encrypted:
            try:
                if not room.members_synced:
                    responses = []
                    responses.append(await self.matrix_client.joined_members(room.room_id))

                if self.matrix_client.olm.should_share_group_session(room.room_id):
                    try:
                        event = self.matrix_client.sharing_session[room.room_id]
                        await event.wait()
                    except KeyError:
                        await self.matrix_client.share_group_session(
                            room.room_id,
                            ignore_unverified_devices=True,
                        )

                if msgtype != "m.reaction":
                    response = self.matrix_client.encrypt(
                        room.room_id, "m.room.message", msgcontent)
                    msgtype, content = response

            except Exception as e:
                self.logger.log(
                    f"Error encrypting message: {e} - sending unencrypted", "error")
                raise

        if not content:
            msgtype = "m.room.message"
            content = msgcontent

        method, path, data = Api.room_send(
            self.matrix_client.access_token, room.room_id, msgtype, content, uuid.uuid4()
        )

        return await self.matrix_client._send(RoomSendResponse, method, path, data, (room.room_id,))

    async def run(self):
        """Start the bot."""

        # Set up the Matrix client

        assert self.matrix_client, "Matrix client not set up"
        assert self.matrix_client.access_token, "Access token not set up"

        if not self.matrix_client.user_id:
            self.matrix_client.user_id = await self._get_user_id()

        if not self.matrix_client.device_id:
            self.matrix_client.device_id = await self._get_device_id()

        # Set up database

        IN_MEMORY = False
        if not self.database:
            self.logger.log(
                "No database connection set up, using in-memory database. Data will be lost on bot shutdown.")
            IN_MEMORY = True
            self.database = DuckDBPyConnection(":memory:")

        self.logger.log("Running migrations...")
        before, after = migrate(self.database)
        if before != after:
            self.logger.log(f"Migrated from version {before} to {after}.")
        else:
            self.logger.log(f"Already at latest version {after}.")

        if IN_MEMORY:
            client_config = AsyncClientConfig(
                store_sync_tokens=True, encryption_enabled=False)
        else:
            matrix_store = DuckDBStore
            client_config = AsyncClientConfig(
                store_sync_tokens=True, encryption_enabled=True, store=matrix_store)
            self.matrix_client.config = client_config
            self.matrix_client.store = matrix_store(
                self.matrix_client.user_id,
                self.matrix_client.device_id,
                self.database
            )

            self.matrix_client.olm = Olm(
                self.matrix_client.user_id,
                self.matrix_client.device_id,
                self.matrix_client.store
            )

            self.matrix_client.encrypted_rooms = self.matrix_client.store.load_encrypted_rooms()

        # Run initial sync
        sync = await self.matrix_client.sync(timeout=30000)
        if isinstance(sync, SyncResponse):
            await self.response_callback(sync)
        else:
            self.logger.log(f"Initial sync failed, aborting: {sync}", "error")
            return

        # Set up callbacks

        self.matrix_client.add_event_callback(self.event_callback, Event)
        self.matrix_client.add_response_callback(self.response_callback, Response)

        # Accept pending invites

        self.logger.log("Accepting pending invites...")
        await self.accept_pending_invites()

        # Start syncing events
        self.logger.log("Starting sync loop...")
        try:
            await self.matrix_client.sync_forever(timeout=30000)
        finally:
            self.logger.log("Syncing one last time...")
            await self.matrix_client.sync(timeout=30000)

    async def process_query(self, room: MatrixRoom, event: RoomMessageText):
        await self.matrix_client.room_typing(room.room_id, True)

        await self.matrix_client.room_read_markers(room.room_id, event.event_id)

        try:
            last_messages = await self._last_n_messages(room.room_id, 20)
        except Exception as e:
            self.logger.log(f"Error getting last messages: {e}", "error")
            await self.send_message(
                room, "Something went wrong. Please try again.", True)
            return

        system_message = self.get_system_message(room)

        chat_messages = [{"role": "system", "content": system_message}]

        for message in last_messages:
            role = "assistant" if message.sender == self.matrix_client.user_id else "user"
            if not message.event_id == event.event_id:
                chat_messages.append({"role": role, "content": message.body})

        chat_messages.append({"role": "user", "content": event.body})

        # Truncate messages to fit within the token limit
        truncated_messages = self._truncate(
            chat_messages, self.max_tokens - 1, system_message=system_message)

        try:
            response, tokens_used = await self.generate_chat_response(truncated_messages)
        except Exception as e:
            self.logger.log(f"Error generating response: {e}", "error")
            await self.send_message(
                room, "Something went wrong. Please try again.", True)
            return

        if response:
            self.logger.log(f"Sending response to room {room.room_id}...")

            # Convert markdown to HTML

            message = await self.send_message(room, response)

            if self.database:
                self.logger.log("Storing record of tokens used...")

                with self.database.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO token_usage (message_id, room_id, tokens, timestamp) VALUES (?, ?, ?, ?)",
                        (message.event_id, room.room_id, tokens_used, datetime.now()))
                    self.database.commit()
        else:
            # Send a notice to the room if there was an error
            self.logger.log("Didn't get a response from GPT API", "error")
            send_message(
                room, "Something went wrong. Please try again.", True)

        await self.matrix_client.room_typing(room.room_id, False)

    async def generate_chat_response(self, messages: List[Dict[str, str]]) -> str:
        """Generate a response to a chat message.

        Args:
            messages (List[Dict[str, str]]): A list of messages to use as context.

        Returns:
            str: The response to the chat.
        """

        self.logger.log(f"Generating response to {len(messages)} messages...")

        response = openai.ChatCompletion.create(
            model=self.model,
            messages=messages,
            api_key=self.openai_api_key
        )

        result_text = response.choices[0].message['content']
        tokens_used = response.usage["total_tokens"]
        self.logger.log(f"Generated response with {tokens_used} tokens.")
        return result_text, tokens_used

    def get_system_message(self, room: MatrixRoom | int) -> str:
        default = self.default_system_message

        if isinstance(room, int):
            room_id = room
        else:
            room_id = room.room_id

        with self.database.cursor() as cur:
            cur.execute(
                "SELECT body FROM system_messages WHERE room_id = ? ORDER BY timestamp DESC LIMIT 1",
                (room_id,)
            )
            system_message = cur.fetchone()

        complete = ((default if ((not system_message) or self.force_system_message) else "") + (
            "\n\n" + system_message[0] if system_message else "")).strip()

        return complete

    def __del__(self):
        """Close the bot."""

        if self.matrix_client:
            asyncio.run(self.matrix_client.close())

        if self.database:
            self.database.close()