# bears-play-by-play

Sends **every play of every Chicago Bears game to your iMessage, live**. It's built for watching from a plane on free in-flight messaging.

A small background service on your Mac checks ESPN's live game feed every 5 seconds. Each new play goes out through the Messages app as soon as it appears:

```
🚨🐻 CHI SCORE! PASSING TOUCHDOWN 🐻🚨
Q1 5:25 · CHI 7 - SEA 0
🐻 CHI 2nd & Goal at SEA 1
(No Huddle, Shotgun) C.Williams pass short right to L.Burden for 1 yard, TOUCHDOWN. C.Santos extra point is GOOD...
```

You also get a "play-by-play is ON" message about 75 minutes before kickoff, a summary at the end of each drive, end-of-quarter notes and the final score. It finds every Bears game on the schedule by itself. There are no dates to set.

## Install (on your Mac, ~2 minutes)

1. Open the **Messages** app and make sure you're signed in to iMessage.
2. Open **Terminal** and paste:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/rodneywhite999/bears-play-by-play/main/install.sh | bash
   ```

3. When it asks, type the phone number (with `+1`) or Apple ID email that should get the messages. Your own number works fine.
4. If macOS asks whether Terminal / python3 can control **Messages**, click **OK**.

You should get two iMessages: a test message and "bot is running". That's it.

## Before you fly ✈️

- [ ] Mac is **on, plugged in, and logged in**. If it's a laptop, keep the **lid open**. The screen can turn off or lock.
- [ ] Mac is connected to Wi-Fi/internet.
- [ ] Turn off automatic macOS update restarts for the trip: System Settings → General → Software Update → Automatic Updates → turn off "Install macOS updates". After a restart the Mac sits at the login screen and the bot can't run.
- [ ] Optional: re-run the install command the day before to confirm you still get the test message.

On the plane, connect to the airline's free messaging Wi-Fi and open your Messages thread.

## Useful commands

| What | Command |
| --- | --- |
| Watch the log live | `tail -f ~/Library/Logs/bears-pbp.log` |
| Send a test message | re-run the install command |
| Update to latest version | re-run the install command |
| Uninstall | `curl -fsSL https://raw.githubusercontent.com/rodneywhite999/bears-play-by-play/main/install.sh \| bash -s -- --uninstall` |
| Dry-run a past game in Terminal | `python3 bears_pbp.py --replay 401873036 --senders console --replay-delay 0.2` |

## Troubleshooting

- **No test message:** open Messages and confirm iMessage is signed in. Then check System Settings → Privacy & Security → Automation and make sure "Messages" is allowed for Terminal and python3. Re-run the installer.
- **"Command Line Tools" popup:** click Install, wait for it to finish, then run the install command again.
- **Messages to yourself don't make a sound:** that's normal for a self-chat. Open the thread to read.

## How it works

- `bears_pbp.py` is plain Python 3.9+ with no packages to install. It uses ESPN's public `site.api.espn.com` scoreboard/summary feed.
- `install.sh` copies it to `~/Library/Application Support/bears-pbp/` and registers a LaunchAgent (`com.bears-pbp.daemon`). The LaunchAgent starts at login, restarts if the bot crashes, and runs under `caffeinate` so the Mac doesn't sleep.
- If the bot starts mid-game (late start or restart), it sends a short catch-up message instead of flooding you with old plays.
- Other channels are also supported through environment variables: WhatsApp through CallMeBot (free, but capped at about 25–50 messages per 4 hours) and Twilio SMS/WhatsApp. See the top of `bears_pbp.py`.
- `python3 tests/simulate_live.py` feeds a finished game to the live tracker a few plays at a time. It checks that every play is sent exactly once.
