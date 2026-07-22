# Security

## Public-release defaults

- No production URL, token, account identifier, chat history, payment record or personal path is included.
- The shared secret is read from `WECHAT_RECEIVER_TOKEN`; inline secrets are rejected by default.
- HTTPS is required. Plain HTTP is accepted only for an explicitly enabled loopback address.
- Existing trigger files form a baseline; startup does not replay historical receipts.
- A receipt needs an explicit timestamp close to the WAL trigger time.
- Full OCR text and screenshots are discarded by default.
- The sample webhook receiver binds to `127.0.0.1` by default and verifies timestamped HMAC signatures.

## Secret handling

Generate a unique random token and set it independently on the agent and receiver. Do not commit it:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Linux environment files and JSON configuration files should be readable only by the service account.

## Privacy

OCR can expose text visible in the selected WeChat window. Keep `include_raw_ocr_text` and `keep_screenshots` disabled unless diagnostics require them. Delete diagnostic captures after use.

## Reporting

Open a private security advisory in the GitHub repository for vulnerabilities. Do not include real tokens, payment records or personal chat screenshots.
