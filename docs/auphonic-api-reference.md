# Auphonic API Reference (Agent-Readable)

> Comprehensive reference for the Auphonic REST API with heavy focus on multitrack production.
> Source: https://auphonic.com/api-docs/ and https://auphonic.com/help/

---

## Base URL

```
https://auphonic.com/api/
```

All resources are referenced by UUID — a 22-character alphanumeric string `[a-zA-Z0-9]{22}`.
Presets can also be referenced by their `preset_name` (most recently created personal preset wins ties).

---

## Authentication

Three methods, all grant full API access:

### 1. Bearer Token / API Key (Recommended for scripts)

Get your key at https://auphonic.com/accounts/api-key/.

```
# Header method
curl https://auphonic.com/api/productions.json \
  -H "Authorization: Bearer {api_key}"

# Query parameter method (for streaming files, images)
curl "https://auphonic.com/api/productions.json?bearer_token={api_key}"
```

### 2. HTTP Basic Auth

```
curl https://auphonic.com/api/productions.json -u username:password
```

Only for personal scripts. Not for third-party apps.

### 3. OAuth 2.0

Required for third-party apps serving multiple users.

**Web App Flow (Authorization Code):**
1. Register app at https://auphonic.com/accounts/register-app/ → get `client_id` and `client_secret`
2. Redirect user to confirmation page with `client_id` and `redirect_uri`
3. Auphonic redirects back with `?code={grant_code}`
4. Exchange grant code for access token:

```
curl -X POST https://auphonic.com/oauth2/token/ \
  -F "client_id={client_id}" \
  -F "client_secret={client_secret}" \
  -F "redirect_uri={redirect_uri}" \
  -F "grant_type=authorization_code" \
  -F "code={grant_code}"
```

Response:
```json
{
  "access_token": "436bfd6bed",
  "token_type": "bearer",
  "expires_in": 315360000,
  "user_name": "my_auphonic_username",
  "scope": ""
}
```

**Password Grant (no redirect):**
```
curl -X POST https://auphonic.com/oauth2/token/ \
  -F "client_id={client_id}" \
  -F "username={username}" \
  -F "password={password}" \
  -F "grant_type=password" \
  -u {client_id}:{client_secret}
```

---

## Standard Response Envelope

Every API response follows this structure:

```json
{
  "status_code": 200,
  "error_code": null,
  "error_message": "",
  "form_errors": {},
  "data": { ... }
}
```

---

## Rate Limits

| Endpoint | Limit |
|---|---|
| GET list/detail endpoints | 60 requests / 4 minutes |
| POST create production | 40 requests / 2 minutes (100 for business) |
| GET status polling | 60 requests / 4 minutes (prefer webhooks) |
| GET download files | 30 downloads / 5 minutes / production |

---

## Production Status Codes

Query all codes: `GET /api/info/production_status.json`

| Code | Status |
|---|---|
| 0 | Incomplete |
| 1 | Not Started |
| 2 | Error |
| 3 | Done |
| 4 | Audio Processing |
| 5 | Waiting |
| 6 | Service Processing |
| 7 | Mono Mixdown |
| 8 | Split on Chapters |
| 9 | Encoding |
| 10 | Waiting for File |
| 11 | Stopped |
| 12 | Not Enough Credits |
| 13 | Queued |
| 14 | File Upload |

---

# API ENDPOINTS — FULL REFERENCE

## Productions

### List Productions
```
GET /api/productions.json
```
| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int >= 1 | 100 | Max results (max 10 for full data) |
| `offset` | int >= 0 | 0 | Skip results |
| `uuids_only` | 0 or 1 | 0 | Return only UUIDs |
| `minimal_data` | 0 or 1 | 0 | Minimal production data |

### Create Production
```
POST /api/productions.json
Content-Type: application/json
```

Core parameters:

| Param | Type | Description |
|---|---|---|
| `is_multitrack` | bool | `true` for multitrack, `false`/omit for singletrack |
| `preset` | string | Preset UUID or name |
| `input_file` | string | Input file URL or filename (singletrack) |
| `service` | string | External service UUID for input file |
| `multi_input_files` | array | Track definitions (multitrack). See Multitrack section |
| `algorithms` | object | Master/singletrack algorithm settings |
| `output_files` | array | Output file format configs |
| `outgoing_services` | array | Services to publish to |
| `metadata` | object | Title, artist, album, tags, etc. |
| `speech_recognition` | object | ASR config |
| `chapters` | array | Chapter marks |
| `webhook` | string (URI) | Webhook callback URL |
| `image` | string | Cover image URL |
| `output_basename` | string | Basename for output files |
| `action` | "start" or "save" | Start processing or just save |
| `review_before_publishing` | bool | Require review before publishing (default false) |
| `cuts` | array of [start, end] | Manual audio cuts in seconds |
| `cut_start` | number | Cut from beginning (seconds) |
| `cut_end` | number | Cut from end (seconds) |
| `reset_data` | bool | Reset all data before applying changes |

### Get Production
```
GET /api/production/{uuid}.json
```

### Update Production
```
POST /api/production/{uuid}.json
Content-Type: application/json
```
Same params as create. Merges with existing data.

### Start Production
```
POST /api/production/{uuid}/start.json
```

### Stop Production
```
POST /api/production/{uuid}/stop.json
```

### Get Production Status
```
GET /api/production/{uuid}/status.json
```
Returns `status` (int) and `status_string`. Prefer webhooks over polling.

### Publish Production (after review)
```
POST /api/production/{uuid}/publish.json
```

### Upload Files
```
POST /api/production/{uuid}/upload.json
Content-Type: multipart/form-data
```

| Field | Description |
|---|---|
| `input_file` | Main audio file (singletrack) |
| `image` | Cover image |
| `intro_file` | Intro audio |
| `outro_file` | Outro audio |
| `{track_id}` | Multitrack file keyed by track ID string |

**IMPORTANT:** Upload requests must be `multipart/form-data`, NOT JSON.

### Set Algorithms
```
PUT /api/production/{uuid}/algorithms.json
```

### Set Metadata
```
PUT /api/production/{uuid}/metadata.json
```

| Param | Type |
|---|---|
| `title` | string |
| `artist` | string |
| `album` | string |
| `track` | string |
| `subtitle` | string (max 255 chars) |
| `summary` | string |
| `genre` | string |
| `year` | string |
| `publisher` | string |
| `url` | string |
| `license` | string |
| `license_url` | string |
| `tags` | array of strings |
| `append_chapters` | bool |
| `location` | object: `{latitude, longitude}` |

### Manage Output Files
```
POST   /api/production/{uuid}/output_files.json     # Add output file
DELETE /api/production/{uuid}/output_files.json     # Remove all
```

Output file object:

| Param | Type | Description |
|---|---|---|
| `format` | string (required) | See Output Formats table |
| `bitrate` | int | Bitrate in kbps |
| `mono_mixdown` | bool | Force mono |
| `split_on_chapters` | bool | One file per chapter (zipped) |
| `suffix` | string | Filename suffix |
| `ending` | string | File extension override |
| `filename` | string | Full filename override |
| `outgoing_services` | array of UUIDs | Services to publish this file to |

### Manage Chapters
```
POST   /api/production/{uuid}/chapters.json     # Add chapter
DELETE /api/production/{uuid}/chapters.json     # Remove all
```

Chapter object:

| Param | Type |
|---|---|
| `start` | string (required) — HH:MM:SS[.mmm] |
| `title` | string (required) |
| `url` | string (optional) |
| `image` | string (optional) — URL or base64 |

### Manage Outgoing Services
```
GET    /api/production/{uuid}/outgoing_services.json
POST   /api/production/{uuid}/outgoing_services.json
DELETE /api/production/{uuid}/outgoing_services.json
```

### Speech Recognition
```
PUT    /api/production/{uuid}/speech_recognition.json
DELETE /api/production/{uuid}/speech_recognition.json
```

Speech recognition object:

| Param | Type | Description |
|---|---|---|
| `type` | string | `"whisper"`, `"google"`, `"wit"`, `"amazon"`, `"speechmatics"` |
| `uuid` | string | External service UUID (not needed for whisper) |
| `language` | string | Language code e.g. `"en-US"` |
| `keywords` | array of strings | Hints/vocabulary for improved accuracy |

### Manage Webhooks
```
GET    /api/production/{uuid}/webhook.json
PUT    /api/production/{uuid}/webhook.json
DELETE /api/production/{uuid}/webhook.json
```

### Manual Audio Cuts
```
POST   /api/production/{uuid}/cuts.json      # Add cuts: {"cuts": [[start, end], ...]}
DELETE /api/production/{uuid}/cuts.json      # Remove all
```

### Download Output Files
```
GET /api/download/{dtype}/{uuid}/{filename}
```
Rate limit: 30 downloads / 5 minutes / production.

---

## Simple API (Singletrack Only)

Single `multipart/form-data` request to create + upload + start.
Cannot be used for multitrack.

```
POST /api/simple/productions.json
Content-Type: multipart/form-data
```

| Param | Type | Default | Description |
|---|---|---|---|
| `preset` | string | — | Preset UUID or name |
| `service` | string | — | External service UUID |
| `input_file` | file or URL | — | Audio file (prefix local files with `@`) |
| `title` | string | — | Title metadata |
| `artist` | string | — | Artist metadata |
| `album` | string | — | Album |
| `track` | string | — | Track number |
| `subtitle` | string | — | Subtitle |
| `summary` | string | — | Description |
| `genre` | string | — | Genre |
| `year` | string | — | Year |
| `publisher` | string | — | Publisher |
| `url` | string | — | URL |
| `license` | string | — | License text |
| `license_url` | string | — | License URL |
| `tags` | string | — | Comma-separated tags |
| `image` | file or URL | — | Cover image |
| `chapters` | file, URL, or string | — | Chapter marks |
| `output_basename` | string | — | Output filename base |
| `filtering` | bool | true | Enable filtering |
| `leveler` | bool | true | Enable leveler |
| `normloudness` | bool | true | Loudness normalization |
| `loudnesstarget` | int | -16 | LUFS target |
| `maxpeak` | float | 0 (auto) | Max true peak dBTP |
| `denoise` | bool | false | Enable noise reduction |
| `denoiseamount` | int | 0 (auto) | Noise reduction dB |
| `silence_cutter` | bool | false | Cut silence |
| `filler_cutter` | bool | false | Cut fillers |
| `cough_cutter` | bool | false | Cut coughs |
| `music_cutter` | bool | false | Cut music |
| `cut_mode` | string | "apply_cuts" | "apply_cuts", "export_uncut_audio", "set_cuts_to_silence" |
| `webhook` | string | — | Webhook URL |
| `action` | string | — | "start" or "save" |

Example:
```bash
curl -X POST https://auphonic.com/api/simple/productions.json \
  -H "Authorization: bearer {api_key}" \
  -F "preset=ceigtvDv8jH6NaK52Z5eXH" \
  -F "title=My Episode" \
  -F "input_file=@/path/to/episode.wav" \
  -F "action=start"
```

---

## Presets

### List Presets
```
GET /api/presets.json
```
| Param | Type | Description |
|---|---|---|
| `limit` | int | Max results |
| `offset` | int | Skip results |
| `uuids_only` | 0 or 1 | UUIDs only |
| `preset_type` | string | "personal_presets", "auphonic_presets", "all_presets" |

### Create Preset
```
POST /api/presets.json
```
Same params as production (no input files, no chapters, no action).
Requires `preset_name`.

### Get / Update / Delete Preset
```
GET    /api/preset/{uuid}.json
PUT    /api/preset/{uuid}.json
DELETE /api/preset/{uuid}.json
```

### Upload Preset Cover Image
```
POST /api/preset/{uuid}/image.json
```

---

## Info Endpoints (No Auth Required)

```
GET /api/info/production_status.json    # All status codes
GET /api/info/output_files.json         # All output formats
GET /api/info/algorithms.json           # All algorithm options
GET /api/info/service_types.json        # All service types
```

## User Info
```
GET /api/user.json    # Current user account info
```

## Services
```
GET /api/services.json                  # List all registered external services
GET /api/service/{uuid}.json            # Get service details
```

---

# MULTITRACK PRODUCTIONS — DETAILED REFERENCE

This is the primary focus section. Multitrack productions take multiple parallel input audio tracks, process them individually and combined, and create a final mixdown automatically.

**Key constraint:** Multitrack is built for speech-dominated programs (podcasts, radio, broadcast, lectures, conferences, screencasts). It is NOT built for music-only productions.

**Requirements:**
- All tracks must start at the same time (or use `offset` to align them)
- One audio source file per track
- Tracks are opened as separate files

## Creating a Multitrack Production

```
POST /api/productions.json
Content-Type: application/json
```

```json
{
  "is_multitrack": true,
  "multi_input_files": [
    {
      "type": "multitrack",
      "id": "host",
      "algorithms": { "denoise": true }
    },
    {
      "type": "multitrack",
      "id": "guest",
      "algorithms": { "denoise": true, "denoiseamount": 12 }
    },
    {
      "type": "multitrack",
      "id": "music",
      "algorithms": {
        "filtering": false,
        "backforeground": "background"
      }
    }
  ],
  "algorithms": {
    "leveler": true,
    "gate": true,
    "crossgate": true,
    "loudnesstarget": -16
  },
  "metadata": { "title": "Episode 42" },
  "output_files": [{ "format": "mp3", "bitrate": 192 }]
}
```

### `multi_input_files` Array — Track Definition

Each track object:

| Param | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | `"multitrack"`, `"intro"`, or `"outro"` |
| `id` | string | yes (for multitrack) | Human-readable track identifier. Used as key for file uploads and as speaker name in transcription |
| `input_file` | string | no | URL or filename (if using external service) |
| `service` | string | no | External service UUID for this track's file |
| `offset` | number | no | Start delay in seconds (positive, default 0) |
| `algorithms` | object | no | Per-track algorithm settings (see below) |

### `is_multitrack` Flag

**MUST** be set to `true` for multitrack productions. Omit or set `false` for singletrack.

---

## Uploading Multitrack Files

After creating the production, upload local files. Use the track `id` as the form field key:

```bash
curl -X POST https://auphonic.com/api/production/{uuid}/upload.json \
  -H "Authorization: bearer {api_key}" \
  -F "host=@/path/to/host-track.wav" \
  -F "guest=@/path/to/guest-track.wav" \
  -F "music=@/path/to/music-track.wav"
```

**IMPORTANT:** Must be `multipart/form-data`, NOT JSON.

### Uploading from External Services or HTTP

Can be set directly in `multi_input_files` without a separate upload step:

```json
{
  "multi_input_files": [
    {
      "type": "multitrack",
      "id": "remote speaker",
      "service": "pmefeNCzkyT4TbRbDmoCDf",
      "input_file": "recording.mp3"
    },
    {
      "type": "multitrack",
      "id": "http speaker",
      "input_file": "https://example.com/recording.mp3"
    },
    {
      "type": "intro",
      "id": "",
      "service": "pmefeNCzkyT4TbRbDmoCDf",
      "input_file": "intro.mp3"
    }
  ]
}
```

When no local file upload is needed, you can create and start in one request with `"action": "start"`.

---

## Starting a Multitrack Production

```bash
curl -X POST https://auphonic.com/api/production/{uuid}/start.json \
  -H "Authorization: bearer {api_key}"
```

Or include `"action": "start"` in the create/update request.

---

## Modifying Tracks

### Update a specific track:
```
POST /api/production/{uuid}/multi_input_files/{track_id}.json
Content-Type: application/json
```
```json
{
  "service": "pmefeNCzkyT4TbRbDmoCDf",
  "input_file": "new_file.mp3",
  "id": "new track name",
  "offset": 5,
  "algorithms": { "denoise": true, "denoiseamount": 12 }
}
```

### Delete all tracks:
```
DELETE /api/production/{uuid}/multi_input_files.json
```

### Delete one specific track:
```
DELETE /api/production/{uuid}/multi_input_files/{track_id}.json
```

### Add tracks to existing production:
```
POST /api/production/{uuid}/multi_input_files.json
```

---

## Per-Track Algorithm Parameters

These go inside the `algorithms` object of each track in `multi_input_files`.

### Adaptive Filtering

| Param | Values | Default | Description |
|---|---|---|---|
| `filtering` | `true`, `false` | `true` | Enable/disable adaptive filtering |
| `filtermethod` | `"hipfilter"`, `"autoeq"`, `"bwe"` | `"hipfilter"` | `hipfilter` = adaptive high-pass. `autoeq` = Voice AutoEQ (includes de-esser, de-plosive, high-pass). `bwe` = Voice AutoEQ + Bandwidth Extension (restores lost high frequencies in low-quality recordings) |

**What they do:**
- **hipfilter:** Classifies lowest wanted signal per segment (male/female speech base, music frequency range) and removes unnecessary low frequencies adaptively. Uses zero-phase linear filtering to avoid asymmetric waveforms.
- **autoeq:** Creates time-dependent spectral EQ profiles per track and per speaker. Creates constant, warm, pleasant sound even with changing voices. Includes de-plosive (reduces "p"/"t" pops) and de-esser.
- **bwe:** Everything in autoeq plus artificially recreates lost higher frequencies from low-quality or compressed audio. Optimized for voice only — does not enhance music, noise, or reverb.

### Noise and Reverb Reduction

| Param | Values | Default | Description |
|---|---|---|---|
| `denoise` | `true`, `false` | `false` | Enable noise reduction |
| `denoisemethod` | `"static"`, `"dynamic"`, `"speech_isolation"`, `"classic"` | `"static"` | Denoising algorithm to use |
| `denoiseamount` | `0` (auto), `3`, `6`, `9`, `12`, ... (dB) | `0` | Max noise reduction. For static: `-1` = disable denoise (dehum only) |
| `dehum` | `0` (auto), `50`, `60` | `0` | Hum base frequency (classic denoiser only) |
| `dehumamount` | `0` (auto), `-1` (disable), `3`, `6`, ... (dB) | `0` | Hum reduction amount (classic denoiser only) |

**Denoising methods explained:**

| Method | Removes | Keeps | Best For |
|---|---|---|---|
| `static` | Reverb + stationary/technical noise | Speech + music + sound effects | Audio drama, guided meditations, keeping ambient sounds. Uses AI noise detection, works with continuous music |
| `dynamic` | Everything except speech and music | Speech + music only | Crowded places, outdoor, large rooms, mouth noises, wind, traffic. AI-based, adaptive per segment |
| `speech_isolation` | Everything except speech (including music) | Speech only | Videos with background music, bar interviews, noisy home recordings |
| `classic` | Broadband background noise + hum | Speech + music + sound effects | Content with hiss/hum but no music. Requires a silent segment for noise profile extraction |

**Per-method sub-controls (for static, dynamic, speech_isolation):**
- **Remove Noise** (`denoiseamount`): Default 100dB (full). Lower to keep some atmosphere.
- **Remove Reverb**: Default 100dB (full). Lower to preserve spatial impression.
- **Remove Breaths**: (dynamic/speech_isolation only) Reduces inhalation/exhalation sounds.

**Recommended combos:**
- Track1 (host, clean mic): `static` — keep sound effects, remove hiss/hum
- Track2 (guest, noisy): `dynamic` — keep music jingles, remove complex noise
- Track3 (phone guest): `speech_isolation` — remove background chatter, wind, music
- Track4 (music): no denoiser

### Advanced Leveler (Per-Track)

| Param | Values | Default | Description |
|---|---|---|---|
| `levelerstrength` | `0` to `100` (%) | `100` | How much leveling. 100% = full, 0% = none. Lower values preserve dynamics. |
| `compressor` | `"auto"`, `"soft"`, `"medium"`, `"hard"`, `"off"` | `"auto"` | Micro-dynamics compression preset |
| `msclassifier` | `"on"`, `"speech"`, `"music"` | `"on"` | Force track type or let classifier decide |
| `pan` | `"L100"`, `"L75"`, `"L50"`, `"L25"`, `"C"`, `"R25"`, `"R50"`, `"R75"`, `"R100"` | `"C"` | Stereo panorama position |

**Compressor presets:**
- **auto:** Medium for strength <=100%, Hard for strength >100%
- **soft:** Less compression
- **medium:** Default sound
- **hard:** More compression, sounds more "processed" / "punchy", compresses extreme spikes
- **off:** No micro-dynamics compression, only macro leveling

### Fore/Background and Ducking (Per-Track)

| Param | Values | Default | Description |
|---|---|---|---|
| `backforeground` | `"auto"`, `"foreground"`, `"background"`, `"unchanged"`, `"ducking"` | `"auto"` | Track role in mix |
| `gain` | `-30` to `+6` (dB) | `0` | Track gain relative to others |
| `backgroundgain` | `-26` to `-3` (dB) | `-18` | Level of background/ducked segments vs foreground |
| `ducking_fadetime` | `125` to `2000` (ms) | `500` | Fade down/up time for ducking transitions |

**Modes explained:**
- **auto:** Auphonic classifies each segment. Speech → foreground, music → classified per clip.
- **foreground:** Always foreground level. Each clip leveled individually (relative differences lost).
- **background:** Each clip mixed at background level (softer than foreground speech).
- **unchanged (foreground):** Level relations within track preserved. Foreground/solo parts match speech level from other tracks.
- **ducking:** Automatically reduces level when speakers in OTHER tracks are active. Good for intros/outros, translated speech, background music.

---

## Master Track Algorithm Parameters

These go in the top-level `algorithms` object (not inside any track).

### Adaptive Leveler (Master)

| Param | Values | Default | Description |
|---|---|---|---|
| `leveler` | `true`, `false` | `true` | Enable master leveler |

### Leveler Broadcast Mode

| Param | Values | Default | Description |
|---|---|---|---|
| `maxlra` | `0` (auto), `5`-`20` (LU) | `0` | Maximum Loudness Range. Low = compressed, high = dynamic. Typical: 3 LU news, 5 LU talk, 5-25 LU features/movies |
| `maxs` | `0` (auto), `3`-`12` (LU relative to target) | `0` | Maximum Short-term Loudness (3s window). Used in advertising regulations. E.g. +5 LU with -23 LUFS target = max -18 LUFS |
| `maxm` | `0` (auto), `8`-`20` (LU relative to target) | `0` | Maximum Momentary Loudness (0.4s window). Used by some broadcasters |

### Adaptive Noise Gate

| Param | Values | Default | Description |
|---|---|---|---|
| `gate` | `true`, `false` | `true` | Decreases volume of segments where speaker is inactive. Reduces noise buildup from multiple open mics |

### Remove Mic Bleed (Crossgate)

| Param | Values | Default | Description |
|---|---|---|---|
| `crossgate` | `true`, `false` | `true` | Removes crosstalk/bleed between microphones. Analyzes which speaker is active and removes correlated signals from other tracks. Decreases ambience and reverb |

### Loudness Normalization and True Peak Limiter

Applied to the FINAL MIXDOWN only.

| Param | Values | Default | Description |
|---|---|---|---|
| `loudnesstarget` | `-13` to `-31` (LUFS) | `-16` | Target loudness. Common: -16 LUFS (podcast/mobile), -23 LUFS (EBU R128 broadcast), -24 LUFS (ATSC A/85) |
| `maxpeak` | `0` (auto), `-0.5`, `-1`, `-1.5`, `-2`, `-3`, `-4`, `-5`, `-6` (dBTP) | `0` | True peak limit. Auto: -1dBTP for targets >= -23, -2dBTP for targets <= -24 |
| `dualmono` | `true`, `false` | `false` | Add -3 LU offset for mono productions (EBU guideline). E.g. -16 target → stereo=-16, mono=-19 |
| `loudnessmethod` | `"program"`, `"dialog"` | `"program"` | Program = whole file loudness. Dialog = measure speech parts only |

**NOTE:** Individual exported tracks are NOT loudness normalized. Loudness normalization is always the last step after final mixdown.

### Automatic Cutting (Master)

| Param | Values | Default | Description |
|---|---|---|---|
| `silence_cutter` | `true`, `false` | `false` | Cut detected silence segments |
| `filler_cutter` | `true`, `false` | `false` | Cut "uhm", "uh", "mh", "ähm", "euh", etc. Works with English, German, Romance languages |
| `cough_cutter` | `true`, `false` | `false` | Cut coughs, throat-clearing, sneezes |
| `music_cutter` | `true`, `false` | `false` | Cut foreground music segments (>20 seconds only) |
| `cut_mode` | `"apply_cuts"`, `"export_uncut_audio"`, `"set_cuts_to_silence"` | `"apply_cuts"` | What to do with detected cuts |

**Cut mode details:**
- **apply_cuts:** Removes cut segments from audio. Chapters and timestamps adjusted accordingly.
- **set_cuts_to_silence:** Fades cut segments to silence (preserves timing). Recommended for video.
- **export_uncut_audio:** Detects and reports cuts but doesn't apply them. Download cut lists for manual editing.

**Multitrack-specific filler cutting behavior:** If another speaker is talking while a filler occurs in one track, the filler is set to silence (not cut) to avoid content loss in the other track.

**Recommended:** Use filler/cough cutting together with Dynamic Denoiser or Speech Isolation to avoid artifacts.

---

## Getting Back Individual Processed Tracks

By default, multitrack produces only the final mixdown. To get individual processed tracks, add a `tracks` output format:

```json
{
  "output_files": [
    { "format": "mp3", "bitrate": 192 },
    { "format": "tracks", "ending": "wav.zip" },
    { "format": "tracks", "ending": "flac.zip" }
  ]
}
```

Available track export formats: `wav.zip` (WAV) or `flac.zip` (FLAC, default). All tracks zipped into one file.

**NOTE:** Exported individual tracks are NOT loudness normalized.

---

## Track Offsets

Each track can have an `offset` in seconds, determining when it starts relative to the first track:

```json
{
  "multi_input_files": [
    { "type": "multitrack", "id": "host", "offset": 0 },
    { "type": "multitrack", "id": "guest", "offset": 1.25 },
    { "type": "multitrack", "id": "music", "offset": 5.1 }
  ]
}
```

`offset` must be a positive number (seconds). Default is 0.

---

## Intro and Outro

Track types `"intro"` and `"outro"` are automatically prepended/appended to the production.

```json
{
  "multi_input_files": [
    { "type": "intro", "id": "", "input_file": "https://example.com/intro.mp3" },
    { "type": "multitrack", "id": "host" },
    { "type": "multitrack", "id": "guest" },
    { "type": "outro", "id": "", "service": "pmefeNCzkyT4TbRbDmoCDf", "input_file": "outro.mp3" }
  ]
}
```

**Important notes:**
- Intros/outros are loudness-normalized only to match the production level — no further processing (no leveling, filtering, noise reduction). Pre-process them yourself.
- Audio and video files supported; video limited to 1 minute.
- For presets, intro/outro must come from HTTP or an external service (not local upload).
- `length` in the response INCLUDES intro/outro duration.

---

## Multitrack Presets

Same as multitrack productions but without input files per track:

```json
{
  "preset_name": "My Podcast Preset",
  "is_multitrack": true,
  "multi_input_files": [
    {
      "type": "multitrack",
      "id": "host",
      "algorithms": { "denoise": true, "denoiseamount": 12 }
    },
    {
      "type": "multitrack",
      "id": "guest",
      "algorithms": { "denoise": true }
    },
    {
      "type": "intro",
      "id": "",
      "input_file": "https://example.com/intro.mp3"
    }
  ],
  "algorithms": {
    "loudnesstarget": -16,
    "leveler": true,
    "gate": true,
    "crossgate": true
  },
  "output_files": [{ "format": "mp3", "bitrate": 192 }]
}
```

Create:
```
POST /api/presets.json
```

---

## Multitrack Production Response

Full response structure from `GET /api/production/{uuid}.json`:

```json
{
  "status_code": 200,
  "error_code": null,
  "error_message": "",
  "form_errors": {},
  "data": {
    "uuid": "WMLwjD4sHJMJV5zEg6oaMh",
    "status": 3,
    "status_string": "Done",
    "is_multitrack": true,
    "creation_time": "2014-08-29T10:07:43.009Z",
    "change_time": "2014-08-29T10:12:19.104Z",
    "length": 118.975986394558,
    "length_timestring": "00:01:58.975",
    "output_basename": "multitrack_test_production",
    "edit_page": "https://auphonic.com/engine/multitrack/edit/{uuid}",
    "status_page": "https://auphonic.com/engine/status/{uuid}",
    "start_allowed": false,
    "change_allowed": true,

    "image": null,
    "thumbnail": null,
    "waveform_image": "https://auphonic.com/api/download/audio-result/{uuid}/waveform.png",
    "metadata": { "title": "...", "artist": "...", ... },
    "chapters": [ ... ],
    "webhook": null,
    "speech_recognition": null,
    "error_status": null,
    "error_message": "",
    "warning_status": null,
    "warning_message": "",
    "outgoing_services": [],

    "multi_input_files": [
      {
        "id": "track 1",
        "type": "multitrack",
        "input_file": "file_1.m4a",
        "input_filetype": "aac",
        "input_channels": 1,
        "input_samplerate": 48000,
        "input_bitrate": 82.701,
        "input_length": 118.975986394558,
        "offset": 0,
        "service": null,
        "algorithms": {
          "filtering": true,
          "denoise": false,
          "denoiseamount": 0,
          "backforeground": "auto"
        }
      },
      {
        "id": "track 2",
        "type": "multitrack",
        "input_file": "File 2.mp3",
        "input_filetype": "mp3",
        "input_channels": 2,
        "input_samplerate": 44100,
        "input_bitrate": 128.0,
        "input_length": 91.6908616780045,
        "offset": 3.5,
        "service": null,
        "algorithms": {
          "filtering": true,
          "denoise": false,
          "denoiseamount": 0,
          "backforeground": "background"
        }
      }
    ],

    "output_files": [
      {
        "format": "mp3",
        "ending": "mp3",
        "suffix": "",
        "filename": "output.mp3",
        "bitrate": 192,
        "mono_mixdown": false,
        "split_on_chapters": false,
        "size": 803635,
        "size_string": "784.8 KB",
        "download_url": "https://auphonic.com/api/download/audio-result/{uuid}/output.mp3",
        "checksum": "1bcb74117e73d00dc469d4364625468e",
        "outgoing_services": []
      }
    ],

    "algorithms": {
      "leveler": true,
      "loudnesstarget": -16,
      "crossgate": true,
      "gate": true
    },

    "statistics": {
      "tracks": [
        {
          "identifier": "track 1",
          "music_speech": "speech",
          "activity": [[0.11, 3.78], [7.93, 15.23]],
          "levels": {
            "loudness": [-17.64, "LUFS"],
            "lra": [3.52, "LU"],
            "max_momentary": [-10.58, "LUFS"],
            "max_shortterm": [-15.51, "LUFS"],
            "signal_level": [-16.2, "dB"],
            "noise_level": [-49.72, "dB"],
            "snr": [33.52, "dB"],
            "gain_mean": [-1.52, "dB"],
            "gain_min": [-61.3, "dB"],
            "gain_max": [2.07, "dB"]
          }
        },
        {
          "identifier": "track 2",
          "music_speech": "speech",
          "activity": [[3.32, 7.73], [12.08, 20.67], [22.22, 28.87]],
          "levels": {
            "loudness": [-34.12, "LUFS"],
            "lra": [9.94, "LU"],
            "max_momentary": [-27.23, "LUFS"],
            "max_shortterm": [-31.27, "LUFS"],
            "signal_level": [-32.52, "dB"],
            "noise_level": [-76.85, "dB"],
            "snr": [44.33, "dB"],
            "gain_mean": [-25.23, "dB"],
            "gain_min": [-98.86, "dB"],
            "gain_max": [17.01, "dB"]
          }
        }
      ],
      "master": {
        "levels": {
          "loudness": [-16.0, "LUFS"],
          "lra": [3.04, "LU"],
          "max_momentary": [-11.44, "LUFS"],
          "max_shortterm": [-14.19, "LUFS"],
          "peak": [-1.0, "dBTP"]
        }
      }
    },

    "used_credits": {
      "onetime": 0.04,
      "recurring": 0.01,
      "combined": 0.05
    }
  }
}
```

### Key Response Fields

| Field | Type | Description |
|---|---|---|
| `is_multitrack` | bool | Always `true` for multitrack |
| `length` | float | Output audio length in seconds. INCLUDES intro/outro |
| `length_timestring` | string | Length as HH:MM:SS.mmm |
| `multi_input_files` | array | Per-track details: file info, algorithms, identifiers |
| `statistics.tracks` | array | Per-track: music_speech classification, activity ranges, level stats |
| `statistics.tracks[].activity` | array of [start, end] | Time ranges (seconds) where speaker/content is active |
| `statistics.tracks[].levels` | object | loudness, LRA, SNR, noise level, gain stats per track |
| `statistics.master` | object | Final mixdown levels: loudness, LRA, peak, momentary, shortterm |
| `used_credits` | object | Credits consumed by this production |

---

## Output File Formats

### Audio Formats

| `format` value | Extension | Notes |
|---|---|---|
| `mp3` | .mp3 | CBR MP3 |
| `mp3-vbr` | .mp3 | Variable bitrate MP3 |
| `aac` | .m4a | AAC (M4A/MP4/M4B) |
| `opus` | .opus | Opus |
| `vorbis` | .ogg | Ogg Vorbis |
| `flac` | .flac | FLAC lossless |
| `alac` | .m4a | Apple Lossless |
| `wav16` | .wav | WAV 16-bit PCM |
| `wav24` | .wav | WAV 24-bit PCM |

### Special Formats

| `format` value | `ending` | Description |
|---|---|---|
| `video` | — | Video output (from video input or generated from audio + cover images) |
| `audiogram` | — | Waveform video with dynamic waveform animation |
| `tracks` | `wav.zip` or `flac.zip` | Individual processed tracks (multitrack only) |
| `unprocessed` | — | Original unprocessed input |
| `chapters` | `chapters.txt`, `psc.xml`, `psc.json`, `pc20.json` | Chapter marks in various formats |
| `image` | — | Cover image in full resolution |
| `description` | `json`, `xml`, `yaml` | Full production description with metadata and stats |
| `statistics` | `txt`, `json`, `yaml` | Audio processing statistics |
| `transcript` | — | Human-readable HTML transcript |
| `speechdata` | `json`, `xml` | Machine-readable speech data |
| `subtitles` | `vtt`, `srt` | Subtitle files |
| `cutlist` | Various (see below) | Cut lists for external editors |

### Cut List Formats

**Region formats** (list cut regions with start/end timestamps):
| `ending` | For Editor |
|---|---|
| `AudacityRegions.txt` | Audacity |
| `AuditionRegions.csv` | Adobe Audition |
| `ReaperRegions.csv` | Reaper |

**EDL formats** (auto-apply cuts on import):
| `ending` | For Editor |
|---|---|
| `SamplitudeReaper.edl` | Samplitude / Reaper |
| `DaVinciResolve.edl` | DaVinci Resolve (CMX3600 compatible) |
| `FinalCutProX.fcpxml` | Final Cut Pro X |
| `FinalCutProLegacy.xml` | Final Cut Pro 7 and earlier |

---

## Webhooks

### Adding a Webhook

Include `webhook` in production or preset creation/update:

```json
{
  "webhook": "https://your-server.com/callback"
}
```

Works in Simple API too:
```bash
-F "webhook=https://example.com/auphonic/webhook"
```

### Webhook Payload

After production finishes, Auphonic sends an HTTP POST to your webhook URL.

**Parameters sent:**

| Param | Type | Description |
|---|---|---|
| `uuid` | string | Production UUID — use to query full details |
| `status` | int | `3` = Done, `2` = Error |
| `status_string` | string | "Done" or "Error" |

**Retry behavior:**
1. First attempt: `application/x-www-form-urlencoded`
2. If that fails (4xx/5xx): retry as `multipart/form-data`
3. Timeout retries on first format

### Deleting a Webhook

Set webhook to empty string:
```json
{ "webhook": "" }
```

---

## Speech Recognition

Available engines: Auphonic Whisper ASR (built-in), Google Cloud Speech, Wit.ai, Amazon Transcribe, Speechmatics.

**Whisper ASR** is recommended — no external account needed, data stays on Auphonic servers, no extra cost, includes speaker diarization.

Add to production:
```json
{
  "speech_recognition": {
    "type": "whisper",
    "language": "en-US"
  }
}
```

For external engines, `uuid` references a registered external service:
```json
{
  "speech_recognition": {
    "type": "google",
    "uuid": "kw7q5CHaGSLgduyMhc9j9K",
    "language": "en-US",
    "keywords": ["Auphonic", "podcast", "specific terms"]
  }
}
```

**Multitrack benefit:** Speaker names from track identifiers are automatically assigned in transcription.

Output formats available: HTML transcript, WebVTT/SRT subtitles, JSON/XML speech data.

**Speech recognition requires a paid Auphonic account.**

---

## Supported Input File Formats

MP2, MP3, MP4, M4A, M4B, M4V, WAV, OGG, OGA, OPUS, FLAC, ALAC, MPG, MOV, AC3, EAC3, AIF, AIFC, AIFF, AIFFC, AU, GSM, CAF, IRCAM, AAC, SND, VOC, VORBIS, VOX, WAVPCM, WMA, ASF, ALAW, APE, MPC, MPC8, MULAW, OMA, RM, TTA, W64, SPX, 3PG, 3G2, 3GPP, 3GP, 3GA, TS, MUS, AVI, DV, FLV, IPOD, MATROSKA, WEBM, MPEG, OGV, VOB, MKV, MK3D, MKA, MKS, QT, MXF.

For lossy formats like MP3, use 192kbps or higher.

---

## Complete Multitrack Workflow Example

### Step 1: Create preset (one-time)
```bash
curl -X POST -H "Content-Type: application/json" \
  https://auphonic.com/api/presets.json \
  -H "Authorization: bearer {api_key}" \
  -d '{
    "preset_name": "Two-Person Podcast",
    "is_multitrack": true,
    "multi_input_files": [
      {
        "type": "multitrack", "id": "host",
        "algorithms": {
          "denoise": true, "denoisemethod": "dynamic",
          "filtermethod": "autoeq",
          "levelerstrength": 100, "compressor": "medium", "pan": "L25"
        }
      },
      {
        "type": "multitrack", "id": "guest",
        "algorithms": {
          "denoise": true, "denoisemethod": "dynamic",
          "filtermethod": "autoeq",
          "levelerstrength": 100, "compressor": "medium", "pan": "R25"
        }
      },
      {
        "type": "intro", "id": "",
        "input_file": "https://example.com/intro.mp3"
      }
    ],
    "algorithms": {
      "leveler": true,
      "gate": true,
      "crossgate": true,
      "loudnesstarget": -16,
      "silence_cutter": true,
      "filler_cutter": true
    },
    "output_files": [
      { "format": "mp3", "bitrate": 192 },
      { "format": "tracks", "ending": "flac.zip" }
    ],
    "speech_recognition": {
      "type": "whisper",
      "language": "en-US"
    },
    "webhook": "https://your-server.com/auphonic-done"
  }'
```

### Step 2: Create production from preset
```bash
curl -X POST -H "Content-Type: application/json" \
  https://auphonic.com/api/productions.json \
  -H "Authorization: bearer {api_key}" \
  -d '{
    "preset": "Two-Person Podcast",
    "metadata": {
      "title": "Episode 42: The Big Discussion",
      "artist": "My Podcast",
      "tags": ["episode42", "discussion"]
    },
    "is_multitrack": true,
    "multi_input_files": [
      { "type": "multitrack", "id": "host" },
      { "type": "multitrack", "id": "guest" }
    ]
  }'
```

Save the returned `uuid`.

### Step 3: Upload track files
```bash
curl -X POST \
  https://auphonic.com/api/production/{uuid}/upload.json \
  -H "Authorization: bearer {api_key}" \
  -F "host=@/path/to/host-recording.wav" \
  -F "guest=@/path/to/guest-recording.wav"
```

### Step 4: Start production
```bash
curl -X POST \
  https://auphonic.com/api/production/{uuid}/start.json \
  -H "Authorization: bearer {api_key}"
```

### Step 5: Wait for webhook or poll status
```bash
# Polling (prefer webhook)
curl https://auphonic.com/api/production/{uuid}/status.json \
  -H "Authorization: bearer {api_key}"
```

### Step 6: Download results
```bash
# Get production details to find download URLs
curl https://auphonic.com/api/production/{uuid}.json \
  -H "Authorization: bearer {api_key}"

# Download the mixdown
curl -o episode42.mp3 \
  "https://auphonic.com/api/download/audio-result/{uuid}/episode42.mp3?bearer_token={api_key}"
```

---

## External Services

Register external services (Dropbox, S3, SFTP, Google Drive, SoundCloud, YouTube, etc.) at:
https://auphonic.com/engine/services/

### List Services
```
GET /api/services.json
```

### Service in Production

Incoming (source file):
```json
{
  "service": "{service_uuid}",
  "input_file": "path/to/file/on/service.mp3"
}
```

Outgoing (publish results):
```json
{
  "outgoing_services": [
    { "uuid": "{service_uuid}" }
  ]
}
```

Service types include: Dropbox, S3, Google Drive, SoundCloud, YouTube, FTP, SFTP, WebDAV, Libsyn, Spreaker, RSS.com, Acast, PodBean, Captivate, Archive.org, and more.

---

## Credits and Account

```
GET /api/user.json
```

Productions report `used_credits` with `onetime`, `recurring`, and `combined` values.

Auphonic uses a freemium model: limited free processing time per month, then subscriptions or pay-as-you-go credits.

---

## Quick Reference: All Multitrack Algorithm Parameters

### Per-Track Parameters

```json
{
  "algorithms": {
    "filtering": true,
    "filtermethod": "hipfilter",
    "denoise": false,
    "denoisemethod": "static",
    "denoiseamount": 0,
    "dehum": 0,
    "dehumamount": 0,
    "levelerstrength": 100,
    "compressor": "auto",
    "msclassifier": "on",
    "pan": "C",
    "backforeground": "auto",
    "gain": 0,
    "backgroundgain": -18,
    "ducking_fadetime": 500
  }
}
```

### Master Track Parameters

```json
{
  "algorithms": {
    "leveler": true,
    "maxlra": 0,
    "maxs": 0,
    "maxm": 0,
    "gate": true,
    "crossgate": true,
    "loudnesstarget": -16,
    "maxpeak": 0,
    "dualmono": false,
    "loudnessmethod": "program",
    "silence_cutter": false,
    "filler_cutter": false,
    "cough_cutter": false,
    "music_cutter": false,
    "cut_mode": "apply_cuts"
  }
}
```

---

## Source Documentation

- API Overview: https://auphonic.com/api-docs/
- Multitrack API: https://auphonic.com/help/api/multitrack.html
- Multitrack Algorithms: https://auphonic.com/help/algorithms/multitrack.html
- Multitrack Web Help: https://auphonic.com/help/web/multitrack.html
- Simple API: https://auphonic.com/help/api/simple_api.html
- Authentication: https://auphonic.com/help/api/authentication.html
- Webhooks: https://auphonic.com/help/api/webhook.html
- Query/Response Details: https://auphonic.com/help/api/query.html
- API Examples: https://auphonic.com/help/api/examples.html
- Full OpenAPI Reference: https://eu1.auphonic.com/help/redoc.html
- External Service Integration: https://auphonic.com/help/api/external_service.html
