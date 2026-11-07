# ATIQ IMAGE HOST

Telegram bot that receives photos and common file attachments, uploads them privately to Amazon S3, and replies with signed Direct, Download, and View links. Hosted as a FastAPI webhook service on Render.

## Features

- Telegram photo, document, video, audio, voice, animation, and video-note uploads
- Amazon S3 private objects (no public bucket access)
- Direct/View (inline) and Download (attachment) presigned URLs
- Webhook secret header validation
- Random S3 object keys; no Telegram user ID in object key
- Environment-variable configuration; no secrets committed to Git
- `/start`, `/help`, `/health`

## Important link behavior

S3 objects are private. The bot creates **presigned URLs**, which expire. The default is 7 days (`URL_EXPIRY_SECONDS=604800`). Direct and View links intentionally use the same inline URL. Download uses a separate URL with attachment disposition.

These are not permanent public links. For permanent public URLs, configure a private S3 bucket with CloudFront signed/public delivery and a custom domain; do not make the entire bucket public.

## 1. Create the Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and follow the prompts.
3. Copy the bot token. Keep it secret.

## 2. Create an S3 bucket

1. In AWS Console, open S3 and create a globally unique bucket.
2. Choose a region (default in this project: `ap-south-1`, Mumbai).
3. Keep **Block all public access** enabled.
4. Keep Object Ownership as **Bucket owner enforced** (ACLs disabled).
5. Enable default encryption (SSE-S3).
6. Create an IAM user or role with only the permissions needed for this bucket. A minimal identity policy is:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBucketOptional",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::YOUR_BUCKET_NAME"]
    },
    {
      "Sid": "ObjectAccessForBot",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": ["arn:aws:s3:::YOUR_BUCKET_NAME/uploads/*"]
    }
  ]
}
```

The bot does not need `s3:ListBucket`; you may remove the optional statement. Never use root-account access keys.

## 3. Deploy to Render

1. Upload this project to a private GitHub repository.
2. In Render, choose **New + → Blueprint** and connect the repository (Render reads `render.yaml`), or create a Web Service manually.
3. Set the environment variables listed below.
4. Set `PUBLIC_BASE_URL` to the exact HTTPS URL Render assigned, e.g. `https://atiq-image-host.onrender.com` (no trailing slash).
5. Set `BOT_TOKEN`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY`.
6. Create long random values for `WEBHOOK_SECRET` and `WEBHOOK_PATH`. Render may generate values for these in Blueprint deployment; verify the path contains only letters, numbers, `_`, and `-`.
7. Deploy. On startup, the app automatically registers the Telegram webhook.
8. Open `https://YOUR-SERVICE.onrender.com/health`; it should return `{"status":"ok"}`.
9. Open your Telegram bot and send `/start`, then test with a small image and a small PDF.

## 4. Environment variables

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Token from @BotFather |
| `PUBLIC_BASE_URL` | Yes | Render HTTPS service URL |
| `WEBHOOK_SECRET` | Yes | 16-256 character Telegram webhook secret |
| `WEBHOOK_PATH` | Yes | Random webhook URL path |
| `S3_BUCKET` | Yes | S3 bucket name |
| `AWS_REGION` | Yes | Bucket region, e.g. `ap-south-1` |
| `AWS_ACCESS_KEY_ID` | Yes | Dedicated IAM access key |
| `AWS_SECRET_ACCESS_KEY` | Yes | Dedicated IAM secret |
| `URL_EXPIRY_SECONDS` | No | 60-604800; default 604800 (7 days) |
| `MAX_FILE_MB` | No | 1-20; default 20 |
| `LOG_LEVEL` | No | Default INFO |

Do not paste credentials into source code, GitHub, Telegram chats, or screenshots. Add secrets only in Render Environment settings.

## 5. Local run (optional)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set the variables in your shell or use a local environment loader, then:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For local webhook testing, you need a public HTTPS tunnel and must set `PUBLIC_BASE_URL` to that tunnel URL. Do not expose a development server without the webhook secret.

## Limits and notes

- Telegram's hosted Bot API currently limits bot file downloads to 20 MB. This project caps files at 20 MB by default.
- Render free services may spin down when idle; the first request after sleep can take longer. Use an always-on plan if you require consistent availability.
- Signed URLs stop working after their expiry time. The object remains in S3 until you delete it.
- This starter project does not include a user/admin dashboard, file deletion commands, quotas, or malware scanning. Add quotas and abuse controls before opening it to the public.
- Avoid accepting executable files or unknown files from untrusted users in a public bot. This bot stores files as opaque objects and does not execute them.

## Troubleshooting

- **403 from Telegram webhook:** check `WEBHOOK_SECRET` matches the value used when registering the webhook; redeploy after changing it.
- **S3 AccessDenied:** verify IAM policy includes `s3:PutObject` and `s3:GetObject` for `arn:aws:s3:::YOUR_BUCKET_NAME/uploads/*`.
- **Invalid bucket/region:** ensure `S3_BUCKET` and `AWS_REGION` match the bucket exactly.
- **Bot does not reply:** check Render logs, confirm the service is live, and verify the bot token.
- **Links fail:** ensure the IAM identity can call `s3:GetObject`, the bucket still exists, and the URL has not expired.

## Project layout

```text
ATIQ_IMAGE_HOST/
├── main.py
├── requirements.txt
├── render.yaml
├── .env.example
├── .gitignore
└── README.md
```
