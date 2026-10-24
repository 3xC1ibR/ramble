# Delivery-app callouts

[historical text removed for privacy]
an authenticated event → Ramble speaks a short callout on the Mac. No AI triage,
agent launch, microphone input, or change to the listening switch is needed.

[historical text removed for privacy]
is teasing; `firm` is available. The shared 90-second cooldown covers both apps
and all devices, including rapid retries and restarts.

[historical text removed for privacy]

[historical text removed for privacy]
- Private token file: `~/.ramble/delivery-webhook.token` (mode 600).
- Login service: `local.ramble.delivery-webhook`, listening only on `127.0.0.1:8789`.
[historical text removed for privacy]
- Logs: `~/.ramble/logs/delivery-webhook.log`.
- Persistent cooldown: `~/.ramble/delivery-webhook.sqlite3`.

The iPhone/iPad must have Tailscale connected to the same tailnet and permission
to reach this Mac on HTTPS port 443. This also works away from home over cellular
or another Wi-Fi network, subject to connectivity and tailnet access rules.
[historical text removed for privacy]
[historical text removed for privacy]

The Mac must be awake, logged in, reachable over Tailscale, and have audible
output volume. Playback uses Ramble's configured Kokoro voice, with its macOS
fallback and shared microphone feedback guard. Sound goes to the Mac's current
audio output (including headphones if selected). The webhook cannot remotely
wake a sleeping Mac or make an iPhone/iPad play audio by itself. It does not turn
ambient listening on. Callouts still work while listening is off.

## Set up each iPhone and iPad

Create **two automations per device**, one for Uber Eats and one for DoorDash.
An automation selects one app so its request can name that app explicitly.

1. Connect Tailscale on the device to the same account/tailnet as the Mac.
2. On the Mac, copy the Authorization header value privately:

   ```sh
   (printf 'Bearer '; cat ~/.ramble/delivery-webhook.token) | pbcopy
   ```

   Paste it into Shortcuts using Universal Clipboard if available, or transfer
   it privately to the device. The value includes `Bearer ` followed by the
   token. Do not put it in the URL or share the completed shortcut publicly.
3. Open **Shortcuts → Automation → + → App**. On versions showing it first,
   choose **Create Personal Automation**.
4. Tap **Choose**, select **Uber Eats**, then **Done**. Select **Is Opened**;
   leave **Is Closed** off. Select **Run Immediately**. On older versions,
   turn **Ask Before Running** off and confirm **Don't Ask**. Tap **Next**.
5. Choose **New Blank Automation** (or **Add Action**), then add **Get Contents
[historical text removed for privacy]
6. Expand the action (**Show More** or the disclosure arrow). Set:

   | Setting | Value |
   |---|---|
   | Method | `POST` |
   | Headers → `Authorization` | The private `Bearer …` value from step 2 |
   | Headers → `Content-Type` | `application/json` |
   | Request Body | `JSON` |
   | JSON text field `app` | `ubereats` |
   | JSON text field `device` | `iphone` (use `ipad` on the iPad) |
   | JSON text field `tone` | `teasing` (or `firm`) |

   These are text fields, not a JSON string placed into a single field.
7. Run the automation once using the editor's play button and allow any network
   or local-network access prompts. **This is the first audible device test**:
   the Mac should speak. Optional during setup: add **Show Result** after the
   URL action to inspect its JSON; remove it afterwards for unattended use.
8. Save with **Done**. Open Uber Eats to test its trigger. If you just used the
   play button, wait 90 seconds first or the duplicate will be suppressed.
9. Repeat steps 3–8 for **DoorDash**, using `app` = `doordash`.
10. Repeat on the other device. Personal automations do not sync between devices.

If your Shortcuts version instead offers an automation trigger inside a
shortcut's editor, create the same URL action, then **Edit → Automation → App**
and choose **Is Opened**. Use the automatic-run/privacy controls offered by that
version. Apple documents both interfaces; labels vary by installed OS version.

For **additional speech on the opening device**, after the URL action add
**Get Dictionary Value** for `status`, then **If** it equals `spoken`, get `text`
from the original URL result and **Speak Text**. The Mac will still speak; the
phone/iPad uses its own system voice and current audio output/volume. This is
optional, requires the webhook to succeed, and has not been tested on a device.
Do not speak the whole JSON response or speak when status is `suppressed`.

## Request and response examples

HTTP request (replace the credential placeholder privately):

```http
POST /ramble-delivery/delivery-open HTTP/1.1
[historical text removed for privacy]
Authorization: Bearer YOUR_PRIVATE_TOKEN
Content-Type: application/json

{"app":"ubereats","device":"iphone","tone":"teasing"}
```

DoorDash from the iPad:

```json
{"app":"doordash","device":"ipad","tone":"firm"}
```

Only `app` is required; `device` is an informational label, not an authentication
identity. Omitting `tone` uses the server setting. Unknown fields and freeform
speech text are rejected. Allowed apps are exactly `ubereats` and `doordash`.

Success is synchronous: HTTP 200, `{"status":"spoken","text":"…",
"playback":"mac","engine":"kokoro","duration_s":3.9}` only after the player
returns successfully. HTTP 200 with `status: suppressed` means cooldown or
playback in progress; no second callout plays. A speech failure is HTTP 503;
the cooldown remains to avoid repeated partial playback. There is no offline
queue or automatic replay after sleep/network failure.

Missing/wrong credentials return 401; malformed/unsupported payloads return
400; wrong content type returns 415; absent/oversized body returns 413; unknown
paths return 404. Authorization is checked before parsing the payload.

[historical text removed for privacy]

```sh
python3 - <<'PY'
from pathlib import Path
from urllib.request import Request, urlopen
token = (Path.home() / '.ramble/delivery-webhook.token').read_text().strip()
request = Request(
[historical text removed for privacy]
    headers={'Authorization': 'Bearer ' + token})
with urlopen(request, timeout=15) as response:
    print(response.status, response.read().decode())
PY
```

## Configure or operate it

Settings live separately in `~/.ramble/delivery-webhook.json`. For example:

```json
{
  "cooldown_s": 120,
  "tone": "firm",
  "callouts": {
[historical text removed for privacy]
[historical text removed for privacy]
  }
}
```

`cooldown_s` accepts 10–3600 seconds. If overriding `callouts`, provide both
nonempty lists; each line is at most 240 characters and may use `{app}`. Keep
[historical text removed for privacy]
overrides the server tone; omit it to follow server changes. Ramble's normal
`~/.ramble/config.json` controls the TTS voice. Settings are loaded at service
startup; restart after changing them:

```sh
launchctl kickstart -k gui/$(id -u)/local.ramble.delivery-webhook
```

Reinstall (preserves the token):

```sh
[historical text removed for privacy]
./bin/install-delivery-webhook
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --yes \
  --set-path=/ramble-delivery http://127.0.0.1:8789
```

To rotate the credential, remove `~/.ramble/delivery-webhook.token`, rerun the
[historical text removed for privacy]
credentials are stored in the repository or launchd plist.

To stop/remove just this integration:

```sh
launchctl bootout gui/$(id -u)/local.ramble.delivery-webhook
rm -f ~/Library/LaunchAgents/local.ramble.delivery-webhook.plist
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=443 \
  --set-path=/ramble-delivery off
```

Disable/delete the device automations as well. Ramble's general `bin/uninstall`
does not remove this separate opt-in service. Do not use `tailscale serve reset`
[historical text removed for privacy]

[historical text removed for privacy]

[historical text removed for privacy]
  `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v`.
  They exercise real HTTP requests, actual `speech.speak` and its SQLite guard,
  invalid auth/payloads, concurrent calls, retry cooldown, restart persistence,
  and speech failures. Synthesis/player processes are replaced in unit tests.
- A separate isolated HTTP integration used **real local Kokoro synthesis**:
  authenticated DoorDash request → valid **24 kHz, 94,196-frame, 3.9248-second
  WAV** → existing `speech.play_audio` → `/usr/bin/afplay` invocation probe.
  The probe verified the feedback guard was committed first. Invalid auth
  returned 401; an Uber Eats/iPad duplicate was suppressed; one player call.
- Evidence and generated WAV are in `/tmp/ramble-delivery-verification/`
  (`verification.json`). This temporary evidence may disappear on cleanup/reboot.
[historical text removed for privacy]
  route**, authenticated health returned 200, missing auth returned 401, and an
  authenticated unsupported app returned 400. Existing Serve routes survived.
- **No audible test playback or microphone access was performed**, following
  repository instructions. The integration proves the audio reaches the guarded
  player invocation, with that process replaced by a probe. Actual speaker
[historical text removed for privacy]
[historical text removed for privacy]

## Official references checked

- [Apple: App Is Opened triggers on opening or switching to an app](https://support.apple.com/guide/shortcuts/setting-triggers-apde31e9638b/9.0/ios/26).
- [Apple: App automations can run without asking](https://support.apple.com/en-euro/guide/shortcuts/-apd602971e63/ios).
- [Apple: Get Contents of URL supports POST with a JSON request body](https://support.apple.com/en-euro/guide/shortcuts/apd58d46713f/9.0/ios/26).
- [Apple: personal automations are specific to each device](https://support.apple.com/en-euro/guide/shortcuts/apd690170742/9.0/ios/26).
- [Apple: alternate automation interface in the currently published guide](https://support.apple.com/en-euro/guide/shortcuts/apdfbdbd7123/10.0/ios/27).
- [Tailscale Serve: private HTTPS access and localhost proxying](https://tailscale.com/docs/features/tailscale-serve),
  [CLI path mounts and background operation](https://tailscale.com/docs/reference/tailscale-cli/serve).
