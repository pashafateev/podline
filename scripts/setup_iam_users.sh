#!/usr/bin/env bash
# One-time IAM setup for Podline's S3 media storage.
#
# Creates two least-privilege IAM users scoped to the podline-media bucket:
#   podline           read/write, no delete — used by pod commands via the
#                     [podline] profile this script writes to ~/.aws/credentials
#   podline-auphonic  read-only — its key goes into Auphonic's external
#                     service form; staged at ~/.aws/podline-auphonic-s3.txt
#
# Secrets are written straight to files with 600 perms and never echoed.
# Requires an authenticated AWS CLI session with IAM permissions (aws login).

set -euo pipefail

BUCKET="podline-media"
CREDENTIALS_FILE="$HOME/.aws/credentials"
AUPHONIC_KEY_FILE="$HOME/.aws/podline-auphonic-s3.txt"

echo "Using AWS identity:"
aws sts get-caller-identity --query Arn --output text

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

cat > "$workdir/rw-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BucketAccess",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::${BUCKET}"
    },
    {
      "Sid": "ObjectReadWriteNoDelete",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    }
  ]
}
EOF

cat > "$workdir/ro-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BucketAccess",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::${BUCKET}"
    },
    {
      "Sid": "ObjectReadOnly",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    }
  ]
}
EOF

ensure_user() {
  local user="$1" purpose="$2"
  if aws iam get-user --user-name "$user" > /dev/null 2>&1; then
    echo "IAM user $user already exists"
  else
    aws iam create-user --user-name "$user" \
      --tags "Key=purpose,Value=$purpose" \
      --query 'User.Arn' --output text
  fi
}

ensure_user podline podline-media-app
aws iam put-user-policy --user-name podline \
  --policy-name podline-media-readwrite \
  --policy-document "file://$workdir/rw-policy.json"

ensure_user podline-auphonic auphonic-external-service
aws iam put-user-policy --user-name podline-auphonic \
  --policy-name podline-media-readonly \
  --policy-document "file://$workdir/ro-policy.json"

mkdir -p "$HOME/.aws"

if grep -q '^\[podline\]' "$CREDENTIALS_FILE" 2>/dev/null; then
  echo "[podline] profile already present in $CREDENTIALS_FILE; not creating a new key"
else
  aws iam create-access-key --user-name podline --output json > "$workdir/podline-key.json"
  python3 - "$workdir/podline-key.json" "$CREDENTIALS_FILE" <<'PYEOF'
import json, os, sys
key = json.load(open(sys.argv[1]))["AccessKey"]
path = sys.argv[2]
with open(path, "a", encoding="utf-8") as handle:
    handle.write(
        f"\n[podline]\naws_access_key_id = {key['AccessKeyId']}\n"
        f"aws_secret_access_key = {key['SecretAccessKey']}\n"
    )
os.chmod(path, 0o600)
print(f"wrote [podline] profile (key id {key['AccessKeyId']}) to {path}")
PYEOF
fi

if [ -f "$AUPHONIC_KEY_FILE" ]; then
  echo "$AUPHONIC_KEY_FILE already exists; not creating a new key"
else
  aws iam create-access-key --user-name podline-auphonic --output json > "$workdir/auphonic-key.json"
  python3 - "$workdir/auphonic-key.json" "$AUPHONIC_KEY_FILE" "$BUCKET" <<'PYEOF'
import json, os, sys
key = json.load(open(sys.argv[1]))["AccessKey"]
path, bucket = sys.argv[2], sys.argv[3]
with open(path, "w", encoding="utf-8") as handle:
    handle.write(
        "Paste these into Auphonic -> Settings -> External Services -> Add Amazon S3 Bucket.\n"
        "Read-only IAM user podline-auphonic; delete this file afterwards.\n\n"
        f"Bucket name:       {bucket}\n"
        f"Access Key ID:     {key['AccessKeyId']}\n"
        f"Secret Access Key: {key['SecretAccessKey']}\n"
    )
os.chmod(path, 0o600)
print(f"staged Auphonic S3 credentials (key id {key['AccessKeyId']}) at {path}")
PYEOF
fi

echo
echo "Done. Next steps:"
echo "  1. Register the bucket in Auphonic using $AUPHONIC_KEY_FILE, then delete that file."
echo "  2. Set 'profile: podline' under storage: in config.local.yaml (or let Claude do it)."
