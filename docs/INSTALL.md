# Install on Fedora Server

This guide installs `uni-maild`, the `uni-mail` command, the dispatcher and,
optionally, `uni-mail-mcp`, on Fedora Server 44 with systemd. It needs no
LLM, agent framework or desktop mail client. To move an existing hermes-mail
installation, follow [MIGRATION.md](MIGRATION.md) instead. To run uni-mail in
rootless Podman containers, see [CONTAINER.md](CONTAINER.md).

uni-mail needs Python 3.14 or later (IMAP IDLE). Fedora Server 44 ships it as
`python3`:

```sh
python3 --version
```

## 1. Install the Python package

From a checkout of this repository:

```sh
sudo dnf install python3
sudo python3 -m venv /opt/uni-mail
sudo /opt/uni-mail/bin/pip install '.[mcp]'      # or '.' without the MCP server
```

The daemon, the CLI and the dispatcher use only the standard library; the
`mcp` extra adds the MCP SDK for `uni-mail-mcp`. With no network access,
install the build backend from Fedora, use `pip install --no-build-isolation .`
and leave out the extra.

For the Nanobot examples, copy the integration scripts too:

```sh
sudo mkdir -p /opt/uni-mail/integrations
sudo cp -r integrations/nanobot /opt/uni-mail/integrations/
```

## 2. Create the users and the directories

```sh
sudo cp deploy/sysusers.d/uni-mail.conf /etc/sysusers.d/
sudo systemd-sysusers /etc/sysusers.d/uni-mail.conf
sudo cp deploy/tmpfiles.d/uni-mail.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/uni-mail.conf
```

This creates:

- the `uni-mail` user and group, which own `/var/lib/uni-mail` and
  `/var/lib/uni-mail/exports` (mode 0750);
- the `uni-mail-dispatch` user, a member of the `uni-mail` group, which owns
  `/var/lib/uni-mail-dispatch` (mode 0700).

## 3. Write the config

```sh
sudo mkdir -p /etc/uni-mail
sudo cp deploy/config.example.json /etc/uni-mail/config.json
sudo chown root:uni-mail /etc/uni-mail/config.json
sudo chmod 0640 /etc/uni-mail/config.json
```

The file has three kinds of settings:

- **Mail** (read by `uni-maild`): `state_dir`, `socket`, `export_dir`,
  `extract_root` (`null` turns `extract-attachment` off),
  `event_retention_days` (default 14, `0` keeps the event log for ever),
  `web_settings` (let socket clients such as `uni-mail account set` change the
  accounts; default `true`) and `accounts.<name>` with `provider`, `address`,
  `auth`, `host`, `port`, `password_file`, `folders`, `archive_folder`,
  `sync_days`, `poll_seconds`, `cache_limit_mb` and `max_part_mb`.
- **Event delivery** (read by `uni-mail dispatch`): the `dispatch` section,
  see [EVENTS.md](EVENTS.md).
- **Integrations** are not in this file: the Nanobot example keeps its own
  settings in the destination's `argv` (see `integrations/nanobot/`).

`archive_folder` defaults to `Archive` for `microsoft` and `[Gmail]/All Mail`
for `google`; the `imap` provider has no default.

Put the password of a password account in its own file that only the
daemon can read. The dispatcher is in the `uni-mail` group, so a password file
must not be readable by the group:

```sh
sudo install -d -m 0700 -o uni-mail -g uni-mail /etc/uni-mail/passwords
sudo install -m 0600 -o uni-mail -g uni-mail /dev/stdin /etc/uni-mail/passwords/selfhosted
```

Put webhook secrets in files that only the dispatcher can read:

```sh
sudo install -d -m 0750 -o root -g uni-mail-dispatch /etc/uni-mail/secrets
openssl rand -hex 32 | sudo install -m 0640 -o root -g uni-mail-dispatch /dev/stdin /etc/uni-mail/secrets/archive-hook
```

## 4. Run the daemon

```sh
sudo cp deploy/uni-mail.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now uni-mail.service
sudo ln -s /opt/uni-mail/bin/uni-mail /usr/local/bin/uni-mail
uni-mail status
```

The unit is hardened: the daemon can reach the network, its socket and its
state directory, and nothing more. It runs no program and sends no webhook.
When you set `extract_root`, add that path to `ReadWritePaths` in a drop-in,
and change `ProtectHome=true` to `ProtectHome=read-only` when it is below
`/home`.

Members of the `uni-mail` group can use the socket `/run/uni-mail/mail.sock`
and read the exported attachments:

```sh
sudo usermod -aG uni-mail <user>     # log in again afterwards
```

## 5. Sign in

```sh
uni-mail auth login university
```

1. Open the printed URL in a browser and sign in, with MFA if the account
   asks for it.
2. The browser goes to a `localhost` URL that does not load. This is expected.
3. Copy the full URL from the address bar and paste it into the command.

The account starts to sync in a few seconds. If a provider later refuses the
refresh token, the account stops and the daemon records one `mail.auth`
event.

### Sending mail

`uni-mail send` submits mail over SMTP with the same refresh token (XOAUTH2).
Gmail needs nothing more: its sign-in scope, `https://mail.google.com/`,
already allows SMTP. A Microsoft account needs the extra scope `SMTP.Send`, so
sign in once more with `--send` (this keeps the IMAP scope):

```sh
uni-mail auth login outlook --send
uni-mail send-check outlook          # signs in to SMTP, sends nothing
```

`send-check` also shows a tenant that has SMTP AUTH turned off (Exchange
Online: `Set-CASMailbox -SmtpClientAuthenticationDisabled $false`, or the
organization-wide setting); then the account cannot send.

Forward a message from the command line with:

```sh
uni-mail forward <mail-id> --account gmail --to tony@example.org [--note "FYI"] [--force]
```

The sender account needs the same SMTP sign-in as `send`. The original is read
with `BODY.PEEK[]` and stays unread.

## 6. Run the dispatcher

Add destinations to the `dispatch` section (see [EVENTS.md](EVENTS.md) and
`integrations/nanobot/README.md`), then:

```sh
sudo -u uni-mail-dispatch /opt/uni-mail/bin/uni-mail dispatch check
sudo cp deploy/uni-mail-dispatch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now uni-mail-dispatch.service
sudo -u uni-mail-dispatch /opt/uni-mail/bin/uni-mail dispatch test <destination>
sudo -u uni-mail-dispatch /opt/uni-mail/bin/uni-mail dispatch status
```

The command destinations run as the dispatcher's user. To run `nanobot
trigger`, which writes the Nanobot workspace, give the unit the Nanobot
user with a drop-in (`systemctl edit uni-mail-dispatch.service`; see
`integrations/nanobot/README.md`).

## 7. The MCP server (optional)

MCP clients start `uni-mail-mcp` over stdio themselves; its user must be in
the `uni-mail` group. For a remote client, run it over Streamable HTTP with a
bearer token, on loopback behind a TLS proxy:

```sh
uni-mail-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --token-file /etc/uni-mail/secrets/mcp-token
```

It refuses to start without a token, or on a non-loopback address without
`--allow-remote`.

The MCP server cannot send mail unless you start it with `--enable-send`,
which adds the `mail_send` tool. It sends only to the recipients that you
list; with no list it can send to nobody:

```sh
uni-mail-mcp ... --enable-send --send-allow tony@example.org --send-account gmail
# or: UNI_MAIL_MCP_SEND_ALLOW=tony@example.org,@example.edu
```

`--send-allow` takes addresses, or `@domain` for every address of a domain
(repeat it, or separate with commas). Other options: `--send-account NAME`
(send only from that account), `--send-rate-limit N` (sends per hour, default
10), `--send-max-body` (20000 characters) and `--send-max-subject` (200).
The same flags govern `mail_forward(mail_id, to, note)`, which forwards one
indexed message with its attachments to the allowlisted recipients. It always
sends from `--send-account` (without that option it refuses), counts against
the same hourly limit, and never forwards one mail to the same address twice.
`force` exists only on the CLI. See [DESIGN.md](DESIGN.md#sending-mail) and
[DESIGN.md](DESIGN.md#forwarding-mail).

## As a user service

uni-mail can also run as a systemd user service of one user, without sudo
after the install and with no shared group: the daemon, the dispatcher and
every client (CLI, MCP, the agent) are that user. This suits a single-user
machine where the agent runs as the same user, as Hermes or Nanobot user
services do. Command destinations then run as that user too.

| | System service | User service |
| --- | --- | --- |
| Program | `/opt/uni-mail` | `~/.local/share/uni-mail/venv` |
| Config | `/etc/uni-mail/config.json` | `~/.config/uni-mail/config.json` |
| State | `/var/lib/uni-mail` | `~/.local/state/uni-mail` |
| Dispatcher state | `/var/lib/uni-mail-dispatch` | `~/.local/state/uni-mail-dispatch` |
| Socket | `/run/uni-mail/mail.sock` | `/run/user/<uid>/uni-mail/mail.sock` |
| Units | `deploy/*.service` | `deploy/user/*.service` |

```sh
python3 -m venv ~/.local/share/uni-mail/venv
~/.local/share/uni-mail/venv/bin/pip install '.[mcp]'
ln -s ~/.local/share/uni-mail/venv/bin/uni-mail ~/.local/bin/uni-mail
install -d -m 0700 ~/.config/uni-mail ~/.config/systemd/user
cp deploy/config.example.json ~/.config/uni-mail/config.json
chmod 0600 ~/.config/uni-mail/config.json
```

In the config, set `state_dir` to `/home/<user>/.local/state/uni-mail`,
`socket` to `/run/user/<uid>/uni-mail/mail.sock`, and `dispatch.state_dir` to
`/home/<user>/.local/state/uni-mail-dispatch` (absolute paths; `~` is not
expanded). Then:

```sh
cp deploy/user/uni-mail.service deploy/user/uni-mail-dispatch.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now uni-mail.service
loginctl enable-linger "$USER"    # run without a login session, and start at boot
uni-mail status
```

Without `--config`, `uni-mail`, `uni-maild` and `uni-mail-mcp` use
`$UNI_MAIL_CONFIG`, else `~/.config/uni-mail/config.json` when it exists, else
`/etc/uni-mail/config.json`; the clients take the socket from that file
unless `--socket` or `$UNI_MAIL_SOCKET` names one. From hermes-mail, use
`sudo uni-mail migrate --user <user>` (docs/MIGRATION.md).

The user units sandbox less than the system units: they leave out the
options that need privileges or user namespaces, and the files are the
user's own anyway. Logs: `journalctl --user -u uni-mail -u uni-mail-dispatch`.

## SELinux

Fedora runs SELinux. When a service does not start, read its log:

```sh
journalctl -u uni-mail.service -u uni-mail-dispatch.service
sudo ausearch -m avc -ts recent
```

When the log shows a denial for `/opt/uni-mail`, fix the file labels:

```sh
sudo restorecon -RFv /opt/uni-mail
```

A command destination below `/home` cannot be started directly by a system
unit (`init_t` may not run `user_home_t` files). Start it through a program
in `/usr/bin`, for example `"argv": ["/usr/bin/python3", "/home/user/script.py"]`,
or install it below `/opt`.

## Move the state to another host

The state directory holds the index, the cache and the sign-in tokens. Copy it
with its modes, and the accounts need no new sign-in:

```sh
sudo systemctl stop uni-mail.service
sudo rsync -a old-host:/var/lib/uni-mail/ /var/lib/uni-mail/
```
