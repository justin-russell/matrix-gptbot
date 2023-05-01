# GPTbot

GPTbot is a simple bot that uses different APIs to generate responses to 
messages in a Matrix room.

It is called GPTbot because it was originally intended to only use GPT-3 to
generate responses. However, it supports other services/APIs, and I will 
probably add more in the future, so the name is a bit misleading.

## Features

- AI-generated responses to all messages in a Matrix room (chatbot)
  - Currently supports OpenAI (tested with `gpt-3.5-turbo`)
- AI-generated pictures via the `!gptbot imagine` command
  - Currently supports OpenAI (DALL-E)
- Mathematical calculations via the `!gptbot calculate` command
  - Currently supports WolframAlpha
- DuckDB database to store spent tokens

## Planned features

- End-to-end encryption support (partly implemented, but not yet working)

## Installation

Simply clone this repository and install the requirements.

### Requirements

- Python 3.10 or later
- Requirements from `requirements.txt` (install with `pip install -r requirements.txt` in a venv)

### Configuration

The bot requires a configuration file to be present in the working directory.
Copy the provided `config.dist.ini` to `config.ini` and edit it to your needs.

## Running

The bot can be run with `python -m gptbot`. If required, activate a venv first.

You may want to run the bot in a screen or tmux session, or use a process
manager like systemd. The repository contains a sample systemd service file
(`gptbot.service`) that you can use as a starting point. You will need to
adjust the paths in the file to match your setup, then copy it to
`/etc/systemd/system/gptbot.service`. You can then start the bot with
`systemctl start gptbot` and enable it to start automatically on boot with
`systemctl enable gptbot`.

## Usage

Once it is running, just invite the bot to a room and it will start responding
to messages. If you want to create a new room, you can use the `!gptbot newroom`
command at any time, which will cause the bot to create a new room and invite
you to it. You may also specify a room name, e.g. `!gptbot newroom My new room`.

Note that the bot will currently respond to _all_ messages in the room. So you
shouldn't invite it to a room with other people in it.

It also supports the `!gptbot help` command, which will print a list of available
commands. Messages starting with `!` are considered commands and will not be
considered for response generation.

## Troubleshooting

**Help, the bot is not responding!**

First of all, make sure that the bot is actually running. (Okay, that's not
really troubleshooting, but it's a good start.)

If the bot is running, check the logs. The first few lines should contain
"Starting bot...", "Syncing..." and "Bot started". If you don't see these
lines, something went wrong during startup. Fortunately, the logs should
contain more information about what went wrong.

If you need help figuring out what went wrong, feel free to open an issue.

**Help, the bot is flooding the room with responses!**

The bot will respond to _all_ messages in the room, with two exceptions:

- Messages starting with `!` are considered commands and will not be considered
  for response generation - regardless of whether the command is valid for the
  bot or not (e.g. `!help` will not be considered for response generation).
- Messages sent by the bot itself will not be considered for response generation.

There is a good chance that you are seeing the bot responding to its own
messages. First, stop the bot, or it will keep responding to its own messages,
consuming tokens.

Check that the UserID provided in the config file matches the UserID of the bot.
If it doesn't, change the config file and restart the bot. Note that the UserID
is optional, so you can also remove it from the config file altogether and the
bot will try to figure out its own User ID.

If the User ID is correct or not set, something else is going on. In this case,
please check the logs and open an issue if you can't figure out what's going on.

## License

This project is licensed under the terms of the MIT license.
