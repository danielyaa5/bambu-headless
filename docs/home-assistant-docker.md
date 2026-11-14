# Home Assistant print controls over SSH and Docker Compose

This guide connects Home Assistant to an existing Linux Docker Compose
deployment of `bambu-headless` to pause, resume and cancel a print.

The command path is:

```text
Home Assistant
  -> SSH using a dedicated key and account
  -> restricted command dispatcher on the Docker host
  -> root-owned control wrapper
  -> Docker Compose service "bambu"
  -> bambu-headless CLI
  -> printer
```

The Home Assistant Bambu Lab integration is not used to dispatch these
commands. It can remain installed for status, sensors and other features.

This does not mean the printer is independent of Bambu services. The
existing `bambu-headless` deployment, saved account configuration and
networking plugin must already work.

## Scope

This guide covers:

- Pause, resume and cancel commands.
- A dedicated SSH account with a restricted command allowlist.
- A wrapper that obtains the current job name before dispatching a command.
- Home Assistant shell commands.
- Optional Home Assistant template buttons and a dashboard example.
- A tested UGOS-specific SSH dispatch pattern.

It does not cover:

- Initial installation or authentication of `bambu-headless`.
- Uploading or starting a print from Home Assistant.
- Printer status sensors.
- Automatic retries.
- General-purpose remote shell access.
- Disabling Bambu Cloud or Handy.

Read [Linux Docker setup](linux-docker.md) and
[CLI commands](commands.md) first.

## Prerequisites

Before adding Home Assistant, verify that the existing Docker deployment
can query the intended printer:

```sh
cd /volume1/docker/bambu-headless
sudo docker compose run --rm --no-deps -T bambu status --json
```

The path above is an example from the tested NAS deployment. Replace it
throughout this guide with the absolute path to your Compose project.

Requirements:

- A working Docker Compose service named `bambu`.
- A working `status --json` command.
- Bash, Python 3, Docker Compose and sudo on the Docker host.
- An SSH server on the Docker host.
- An SSH client available in the Home Assistant Core execution environment.
- A dedicated SSH account, called `ha-bambu` in this guide.
- A supported printer selected by the existing Compose configuration.

For deployments with several saved printers, configure an explicit printer
selection using the project's documented settings before proceeding.
Do not rely on automatic selection when it is ambiguous.

Keep the real Bambu Studio account region. Do not change account region,
password or saved credentials to implement these controls.

If an older checkout rejects a valid Europe configuration, review the
current project's region support and rebuild the image as appropriate.
Changing source files on the host does not update an image that copied
those files during its build.

## Security model

The SSH account must not receive unrestricted Docker or sudo access.

The intended restrictions are:

- Home Assistant uses a dedicated SSH key.
- Printer-control dispatch accepts only three exact command strings.
  Other SSH capabilities, including file transfer and forwarding, require
  separate restrictions; see the platform-specific notes below.
- The wrapper accepts exactly one action: `pause`, `resume` or `cancel`.
- The wrapper obtains the job name itself.
- Home Assistant cannot supply arbitrary CLI arguments or job names.
- The wrapper and Compose deployment are controlled by an administrator.
- Bambu credentials remain on the Docker host.

The wrapper runs Docker as root through sudo. Treat the wrapper, Compose
file, `.env`, Dockerfile, source tree and mounted configuration as
privileged assets. The SSH account must not be able to modify them or
replace files through writable parent directories.

Do not add the SSH account to the Docker group. Do not make `.env`
world-readable to solve a permissions error.

Possession of the Home Assistant SSH key permits printer controls.
Protect that key and Home Assistant backups accordingly.

## 1. Install the control wrapper on the Docker host

Create `/usr/local/bin/bambu-ha-control` as an administrator:

```bash
#!/usr/bin/bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: bambu-ha-control {pause|resume|cancel}" >&2
    exit 2
fi

ACTION="$1"

case "$ACTION" in
    pause|resume|cancel) ;;
    *)
        echo "Usage: bambu-ha-control {pause|resume|cancel}" >&2
        exit 2
        ;;
esac

cd /volume1/docker/bambu-headless

STATUS="$(
    /usr/bin/docker compose run --rm --no-deps -T \
        bambu status --json
)"

JOB="$(
    printf '%s\n' "$STATUS" |
        /usr/bin/python3 -c '
import json
import sys

job = json.load(sys.stdin).get("job")
if not isinstance(job, str) or not job:
    raise SystemExit("No identifiable job was returned.")
print(job, end="")
'
)"

exec /usr/bin/docker compose run --rm --no-deps -T \
    bambu "$ACTION" --job "$JOB" --yes
```

This is the wrapper used in the validated UGOS deployment. Before
installing it elsewhere, check the absolute paths to Bash, Docker and
Python 3, and replace the Compose project directory as necessary.
Changes to these paths are deployment-specific adaptations.

Set ownership and permissions:

```sh
sudo chown root:root /usr/local/bin/bambu-ha-control
sudo chmod 755 /usr/local/bin/bambu-ha-control
sudo /usr/bin/bash -n /usr/local/bin/bambu-ha-control
```

Use the appropriate Bash path if your host differs from the tested
deployment.

The script deliberately:

- Rejects missing, additional and unsupported arguments.
- Uses absolute paths for Docker and Python 3.
- Uses `set -euo pipefail` so a failed status query or JSON-processing
  command stops execution before the control command.
- Rejects missing, empty or non-string job names.
- Preserves the returned job name rather than trimming or reconstructing it.
- Passes the job name as one quoted argument.
- Uses `-T` so Compose does not allocate a pseudo-terminal.
- Leaves printer-state validation and final job matching to the CLI.
- Does not retry a control command.

Malformed JSON or an unexpected top-level JSON value causes the
JSON-processing command to fail. This wrapper does not provide custom
messages for every parsing failure and does not explicitly reject
whitespace-only names or control characters.

Bash command substitution removes trailing newline characters. Ordinary
job names are preserved, including spaces and punctuation; this wrapper
should not be treated as supporting arbitrary control characters in names.

A reported job name is not proof that a print is currently running.
Printers may retain the name of a finished or failed job. The CLI must
still decide whether the requested action is valid in the current state.

The initial status query and control command are separate operations.
This is not an atomic transaction, and a job name is not a unique
per-print identifier. Check the printer if the outcome is uncertain.

## 2. Configure narrowly scoped sudo access

Create the sudoers file using `visudo`:

```sh
sudo visudo -f /etc/sudoers.d/bambu-ha-control
```

A command-specific rule is:

```sudoers
ha-bambu ALL=(root) NOPASSWD: /usr/local/bin/bambu-ha-control pause, /usr/local/bin/bambu-ha-control resume, /usr/local/bin/bambu-ha-control cancel
```

Then validate it:

```sh
sudo chmod 440 /etc/sudoers.d/bambu-ha-control
sudo visudo -c
```

This rule is narrower than the rule used during the live UGOS validation:

```sudoers
ha-bambu ALL=(root) NOPASSWD: /usr/local/bin/bambu-ha-control
```

The broader tested rule relied on the root-owned wrapper to reject
unsupported arguments. The command-specific alternative above was not
part of that end-to-end hardware validation.

Do not grant passwordless access to Docker, a shell, or arbitrary scripts.

## 3. Restrict SSH command dispatch

Installing a public key alone is not a command restriction.

Configure the server so that the dedicated account can dispatch only:

```text
bambu-ha-control pause
bambu-ha-control resume
bambu-ha-control cancel
```

Reject other command strings, including extra arguments.

### UGOS: tested dispatch pattern

The tested NAS already used `/etc/ssh/force_command.sh` to dispatch SSH
sessions. Ordinary users were otherwise restricted to file-transfer
operations.

Before changing this file:

1. Keep a separate administrator session open.
2. Save a backup.
3. Review the existing dispatcher and its ordering.
4. Do not replace the vendor file with an unrelated generic script.

The account-specific allowlist used this dispatch pattern:

```bash
if [ "$USER" = "ha-bambu" ]; then
    case "$SSH_ORIGINAL_COMMAND" in
        "bambu-ha-control pause"|"bambu-ha-control resume"|"bambu-ha-control cancel")
            exec /usr/bin/sudo -n /usr/local/bin/bambu-ha-control \
                "${SSH_ORIGINAL_COMMAND##* }"
            ;;
    esac
fi
```

Place the account-specific handling before the existing administrator
and default handling.

In the tested arrangement, unmatched commands fell through to the
existing vendor restrictions. That is not equivalent to a standalone
deny-all dispatcher: any file-transfer handling earlier in the vendor
script must also be considered.

For a dedicated control account, an explicit rejection branch can prevent
unmatched commands from reaching later handlers:

```bash
if [ "$USER" = "ha-bambu" ]; then
    case "$SSH_ORIGINAL_COMMAND" in
        "bambu-ha-control pause"|"bambu-ha-control resume"|"bambu-ha-control cancel")
            exec /usr/bin/sudo -n /usr/local/bin/bambu-ha-control \
                "${SSH_ORIGINAL_COMMAND##* }"
            ;;
        *)
            echo "Command not permitted." >&2
            exit 126
            ;;
    esac
fi
```

The explicit-rejection variant is a hardening example, not a claim about
the exact contents of the tested vendor dispatcher. Its placement must
be reviewed in the context of the complete script, including any earlier
SFTP handling.

Check that `/usr/bin/sudo` is the correct absolute path on your host.

This is an UGOS-specific pattern, not a portable OpenSSH configuration.
Vendor updates may change or replace this file. Recheck the restriction
after NAS updates.

### Other OpenSSH servers

Use an administrator-reviewed forced-command dispatcher, for example
through an appropriate `Match User`/`ForceCommand` configuration or a
per-key forced command.

The dispatcher must inspect `SSH_ORIGINAL_COMMAND`, allow only the three
exact strings above and reject everything else. Also restrict forwarding,
PTY allocation and unrelated session capabilities.

Do not assume an existing server-wide `ForceCommand` and a per-key forced
command will combine in the way you intend.

A generic OpenSSH deployment was not part of the UGOS hardware validation
described below. Follow your platform's OpenSSH documentation and test
both permitted and rejected commands before using printer controls.

## 4. Create the Home Assistant SSH key

Use a dedicated key stored in Home Assistant's persistent configuration
directory.

If the following key already exists and is used by another integration,
do not overwrite it. Choose a separate filename and update the examples.

From the Terminal & SSH app, for example:

```sh
mkdir -p /config/.ssh
chmod 700 /config/.ssh

ssh-keygen -t ed25519 \
    -f /config/.ssh/id_ed25519 \
    -C "homeassistant-bambu"

chmod 600 /config/.ssh/id_ed25519
```

For unattended use, the key must work without an interactive passphrase
prompt in the Home Assistant Core execution environment.

Install only the public key from:

```text
/config/.ssh/id_ed25519.pub
```

in the dedicated account's authorized keys on the Docker host, using the
account's actual home directory and the platform's required permissions.

Never copy the private key into GitHub, the Compose project or a support
message.

## 5. Verify and pin the SSH host key

Do not disable host-key checking.

Obtain the Docker host's SSH host public key or fingerprint through a
trusted administrator session. On the tested NAS, the Ed25519 host public
key was available at:

```sh
sudo cat /etc/ssh/ssh_host_ed25519_key.pub
```

Create `/config/.ssh/known_hosts` in Home Assistant using the verified
key. For an SSH server on the default port, an entry has this form:

```text
192.168.2.2 ssh-ed25519 REPLACE_WITH_VERIFIED_HOST_PUBLIC_KEY
```

Replace the IP address and placeholder with your own verified values.
The placeholder is not a usable key.

Then set:

```sh
chmod 600 /config/.ssh/known_hosts
```

A key obtained using `ssh-keyscan` still needs independent verification.
Do not blindly trust a newly scanned key.

The examples below use the default SSH port. A non-default port requires
both the appropriate SSH option and the corresponding known-hosts entry.

## 6. Add Home Assistant shell commands

In `configuration.yaml`:

```yaml
shell_command: !include shell_command.yaml
```

If `shell_command` already exists, merge the configuration instead of
creating a duplicate top-level key.

Create or extend `shell_command.yaml`:

```yaml
bambu_pause: >-
  ssh -i /config/.ssh/id_ed25519
  -o UserKnownHostsFile=/config/.ssh/known_hosts
  -o StrictHostKeyChecking=yes
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o ConnectTimeout=10
  ha-bambu@192.168.2.2
  'bambu-ha-control pause'

bambu_resume: >-
  ssh -i /config/.ssh/id_ed25519
  -o UserKnownHostsFile=/config/.ssh/known_hosts
  -o StrictHostKeyChecking=yes
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o ConnectTimeout=10
  ha-bambu@192.168.2.2
  'bambu-ha-control resume'

bambu_cancel: >-
  ssh -i /config/.ssh/id_ed25519
  -o UserKnownHostsFile=/config/.ssh/known_hosts
  -o StrictHostKeyChecking=yes
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o ConnectTimeout=10
  ha-bambu@192.168.2.2
  'bambu-ha-control cancel'
```

Replace the host address and account if necessary.

Each value in `shell_command.yaml` is a command string. Do not wrap it in
a `command:` dictionary.

Check the Home Assistant configuration and restart Home Assistant to
load the configuration.

These entries expose actions:

```text
shell_command.bambu_pause
shell_command.bambu_resume
shell_command.bambu_cancel
```

They do not automatically create `button.*` entities.

### Home Assistant execution environment

A command working in the Terminal & SSH app is not sufficient proof that
it works from Home Assistant Core.

Test the action from Developer Tools > Actions. The explicit key and
known-hosts paths avoid relying on the app's SSH home directory or
session environment.

Home Assistant terminates `shell_command` processes after 60 seconds.
`ConnectTimeout=10` limits SSH connection establishment; it does not limit
the full remote operation.

A timeout is an uncertain outcome, not proof that no command reached the
printer. Inspect the printer or Handy before retrying.

## 7. Optional template button entities

Add the following to `configuration.yaml`, merging it into an existing
`template` section if one is already present:

```yaml
template:
  - button:
      - name: Bambu Pause Print
        unique_id: bambu_headless_pause_print
        icon: mdi:pause
        press:
          - action: shell_command.bambu_pause

      - name: Bambu Resume Print
        unique_id: bambu_headless_resume_print
        icon: mdi:play
        press:
          - action: shell_command.bambu_resume

      - name: Bambu Cancel Print
        unique_id: bambu_headless_cancel_print
        icon: mdi:stop
        press:
          - action: shell_command.bambu_cancel
```

If templates are maintained in an included file, adapt the nesting to the
existing include structure. Do not paste another top-level `template:`
inside a file that already represents the contents of that section.

Check the configuration and reload template entities or restart Home
Assistant as appropriate.

Confirm the actual entity IDs in Home Assistant before using them.
Entity IDs can differ if an entity already uses the proposed name.

These buttons dispatch commands. They do not expose printer state,
progress or command confirmation as sensor entities.

## 8. Optional dashboard controls

The following example assumes the generated entity IDs match those
shown here:

```yaml
type: horizontal-stack
cards:
  - type: button
    entity: button.bambu_pause_print
    name: Pause
    icon: mdi:pause
    show_state: false
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.bambu_pause_print
    hold_action:
      action: none

  - type: button
    entity: button.bambu_resume_print
    name: Resume
    icon: mdi:play
    show_state: false
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.bambu_resume_print
    hold_action:
      action: none

  - type: button
    entity: button.bambu_cancel_print
    name: Cancel
    icon: mdi:stop
    show_state: false
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.bambu_cancel_print
      confirmation:
        text: Cancel the current print?
    hold_action:
      action: none
```

The cancel confirmation belongs to this dashboard card only.

Calling `button.press`, invoking `shell_command.bambu_cancel`, or using
an automation bypasses that dashboard confirmation.

Do not expose cancellation through unattended automations unless that
behavior is intentional.

## 9. Test without starting a print

First verify argument rejection on the Docker host:

```sh
sudo /usr/local/bin/bambu-ha-control
sudo /usr/local/bin/bambu-ha-control invalid
sudo /usr/local/bin/bambu-ha-control pause extra
```

Each call should fail before querying the printer or dispatching a
control command.

Next, with the printer powered on and idle, call from Home Assistant
Developer Tools > Actions:

```yaml
action: shell_command.bambu_pause
data: {}
```

Possible safe outcomes include:

- The wrapper rejects a missing or unusable job name.
- The CLI rejects pause because the printer is not in a valid state.

A state-related rejection after remote execution is different from an
SSH failure. It can demonstrate that the command path reached the CLI,
but it does not prove that pause works during a real print.

Also test that an unrelated SSH command is rejected:

```sh
ssh -i /config/.ssh/id_ed25519 \
    -o UserKnownHostsFile=/config/.ssh/known_hosts \
    -o StrictHostKeyChecking=yes \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    ha-bambu@192.168.2.2 \
    'id'
```

This must not return an unrestricted shell command result.

## 10. Optional live validation

Use a supervised, disposable test print.

1. Start the test print through your normal workflow.
2. Call `shell_command.bambu_pause`.
3. Confirm on the printer or Handy that printing actually pauses.
4. Call `shell_command.bambu_resume`.
5. Confirm that the same print resumes.
6. Only if the print can be discarded, call
   `shell_command.bambu_cancel`.
7. Confirm cancellation on the printer or Handy.

Do not infer success solely from a button animation or an SSH connection.

For cancellation, explicitly decide that the test print can be lost.
There is no automatic recovery of the cancelled job in this workflow.

## Troubleshooting

### `Host key verification failed`

Confirm that Home Assistant Core can read:

```text
/config/.ssh/known_hosts
```

Verify that the entry matches the host and SSH port used by the command.

Do not work around this by setting `StrictHostKeyChecking=no`.
If the host key changed, verify the new key through a trusted channel.

### `Permission denied (publickey)`

Check:

- The dedicated account and its authorized keys.
- Ownership and permissions of the SSH files.
- The private-key path in Home Assistant.
- Whether the server permits this account to authenticate.
- Whether the forced-command configuration is compatible with the
  platform's existing SSH restrictions.

Do not enable unrestricted shell access merely to bypass the dispatcher.

### `sudo: a password is required`

Check the sudoers rule, wrapper path and command arguments.

The dispatcher uses `sudo -n`, so it fails instead of waiting for an
interactive password prompt.

### `.env: permission denied`

Docker Compose is being run as an account that cannot read the project.

In this design, the restricted SSH account invokes the root-owned wrapper
through sudo, and the wrapper runs Compose as root.

Do not insert `sudo -u ha-bambu` before Docker inside the wrapper.
Do not make `.env` public.

### Invalid JSON or a failed status query

Run the read-only command as an administrator:

```sh
cd /volume1/docker/bambu-headless
sudo docker compose run --rm --no-deps -T bambu status --json
```

Resolve the underlying status/configuration problem before testing a
control command.

The wrapper must not continue after a failed query or parse failure.
The installed wrapper may show a Python traceback for malformed JSON
or an unexpected top-level JSON value.

### `Current job does not match --job; control refused`

Do not hard-code a previous job name.

The wrapper obtains the current name and passes it as a quoted argument.
A mismatch can still occur if the printer changes jobs between the
status query and the control operation.

Inspect the current printer state before making another attempt.

### `Pause is not valid in the current printer state`

The printer may be idle, finished, failed or already paused.

This is a printer-state rejection, not necessarily a failure of SSH,
sudo or Docker.

### The action times out

Home Assistant has a fixed 60-second `shell_command` timeout.

Inspect the printer before retrying. The remote command or container may
have progressed even if Home Assistant did not receive a final result.

Do not add an automatic retry loop for pause, resume or cancel.

### SSH prints a post-quantum key-exchange warning

A key-exchange warning alone does not establish whether the printer
command succeeded or failed.

Evaluate the command result separately. Review SSH client/server upgrade
options with the host administrator rather than disabling host-key
verification.

## Validation record

Live deployment validation was completed on 13 November 2026 using:

- UGREEN NAS running UGOS 1.21.1.0016.
- Linux Docker Compose.
- Bambu Lab P1S, firmware 01.10.00.00.
- Bambu networking plugin 02.08.02.54.
- Bambu Studio account region: Europe.
- Bambu Cloud and Handy left enabled.

The deployment validation included:

- Bash syntax checking.
- Rejection of missing, invalid and additional wrapper arguments.
- Eight tests using a simulated Docker executable, covering status-query
  failures, invalid JSON, missing/empty/non-string job names and
  preservation of control arguments.
- A live query without an identifiable job, which stopped before
  dispatching a control command.
- A real print started from Bambu Handy.
- Pause from Home Assistant, confirmed by the CLI with exit code 0 and
  independently observed.
- Resume through a Home Assistant button entity, independently observed.
- Cancel through a Home Assistant button entity while printing, confirmed
  by the user.

Validation boundaries:

- The live deployment used UGOS-specific SSH command restrictions.
- The wrapper shown in this guide was checked against the installed
  wrapper after live validation.
- The generic OpenSSH alternative was not tested in that deployment.
- The command-specific sudoers alternative was not part of the live test.
- The explicit-rejection SSH dispatcher variant is a hardening example,
  not a byte-for-byte record of the tested vendor dispatcher.
- The tests do not establish that every printer model or firmware behaves
  identically.
- They do not constitute validation of a particular upstream Europe
  region-support commit.
- The illustrative Home Assistant entity names and dashboard examples
  should not be treated as a byte-for-byte record of the tested deployment.

## Further reading

- [Linux Docker setup](linux-docker.md)
- [CLI commands](commands.md)
- [Project validation notes](validation.md)
- [Home Assistant shell commands](https://www.home-assistant.io/integrations/shell_command/)
- [Home Assistant template entities](https://www.home-assistant.io/integrations/template/)
- [OpenSSH sshd configuration](https://man.openbsd.org/sshd_config)
- [OpenSSH authorized keys](https://man.openbsd.org/sshd.8)
