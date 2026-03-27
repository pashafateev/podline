# Open Issues

## Evaluate Auphonic external-service inputs

Question
- Should the workflow support uploading spoken-track WAVs to an external service first and then creating the Auphonic multitrack production from remote inputs instead of direct local multipart upload?

Why this is worth investigating
- Local uploads have been flaky on macOS due to intermittent TLS/curl transport failures.
- External-service inputs could make the trigger path more robust and allow someone else to kick off a production without relying on the local machine upload path.

Questions to answer before implementing
- Which backing store fits best: S3, S3-compatible storage, Dropbox, SFTP, or signed HTTPS URLs?
- How would remote file staging fit into the rest of the workflow after Auphonic finishes?
- Where would credentials live, and who would manage them?
- Would this add infrastructure that is disproportionate to the reliability gain?
- How would the post-Auphonic steps continue if a different machine or user initiated the job?

Decision criteria
- More reliable than direct local multipart upload
- Fits the rest of the production workflow without awkward manual handoffs
- Reasonable operational burden for credentials, storage, and cleanup
- Keeps the automation understandable for future maintainers
