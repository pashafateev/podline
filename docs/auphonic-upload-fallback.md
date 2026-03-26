# Auphonic Upload Fallback

When direct local multipart uploads to Auphonic are unreliable, use an external-service input path instead of streaming WAVs from the local machine.

Why this exists
- Local uploads from macOS can intermittently fail with TLS/curl transport errors.
- Auphonic supports creating multitrack productions that reference files from external services or HTTP URLs.
- This avoids the fragile local upload step entirely.

Recommended fallback flow
1. Upload each spoken-track WAV to a stable remote location.
2. Create or update the Auphonic production with `multi_input_files` that point to those remote files.
3. Start the production once Auphonic reports all tracks are attached.

Good backing stores
- S3 or S3-compatible object storage
- Dropbox
- SFTP-backed service configured in Auphonic
- Signed HTTPS URLs if the files can be fetched directly

Shape of the request
```json
{
  "preset": "<preset_uuid>",
  "is_multitrack": true,
  "multi_input_files": [
    {
      "type": "multitrack",
      "id": "Jonathan",
      "service": "<service_uuid>",
      "input_file": "path/to/010124_012430_Tr1.WAV"
    },
    {
      "type": "multitrack",
      "id": "Pasha",
      "service": "<service_uuid>",
      "input_file": "path/to/010124_012430_Tr2.WAV"
    }
  ]
}
```

Notes
- The `id` values must match the multitrack preset slot ids.
- For direct HTTPS inputs, `input_file` can be a URL instead of a service/path pair.
- This is the preferred backup strategy if local `upload.json` calls remain flaky.
