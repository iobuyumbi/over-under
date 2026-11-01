# secrets/ -- encrypted .env backup

`env_bundle.zip.enc` is an **AES-256-CBC encrypted** copy of the project's
`.env` files. It is safe to keep in this **public** repository because the
contents cannot be read without the passphrase.

Nothing plaintext is ever committed. The real `.env` files stay gitignored
(see `.gitignore` line 16).

## What is inside the bundle

| File in the archive            | Source project                          |
| ------------------------------ | --------------------------------------- |
| `over-under.env`               | `over-under/.env`                       |
| `overunder-v3.env`             | `overunder-v3/.env`                     |
| `overunder-v2.env`             | `overunder-v2/.env`                     |
| `over-under-cl__over-under-main.env` | `over-under-cl/over-under-main/.env` |

Core secrets shared by all four: `GITHUB_PAT`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `TELEGRAM_VIP_CHAT_ID`. `overunder-v2.env` also carries
tuning variables (`MARKET_*`, `OU_*`, `ST_*`).

## Decrypt

The passphrase is **not** stored in this repo. Keep it somewhere safe
(password manager / Trae memory).

```powershell
# from the repository root
$env:ENV_PASSPHRASE = '<your passphrase>'
.\secrets\env_crypto.ps1 -Mode decrypt `
  -In  .\secrets\env_bundle.zip.enc `
  -Out .\env_bundle.zip
Expand-Archive .\env_bundle.zip -DestinationPath .\env_restore -Force
# rename each file back (e.g. over-under.env -> over-under/.env) as needed
```

If `ENV_PASSPHRASE` is unset the script prompts you with hidden input.

## Re-encrypt (e.g. after rotating a token or changing the passphrase)

```powershell
# 1. put the current .env files into a staging folder using the archive names above
# 2. zip the staging folder
Compress-Archive -Path .\staging\* -DestinationPath .\env_bundle.zip -Force
# 3. encrypt with the new passphrase
$env:ENV_PASSPHRASE = '<new passphrase>'
.\secrets\env_crypto.ps1 -Mode encrypt `
  -In  .\env_bundle.zip `
  -Out .\secrets\env_bundle.zip.enc
# 4. delete the plaintext zip
Remove-Item .\env_bundle.zip
```

## File format

```
[16-byte random salt][16-byte random IV][AES-256-CBC/PKCS7 ciphertext]
key = PBKDF2-HMAC-SHA1(passphrase, salt, 200000 iterations, 32 bytes)
```

## Security notes

- **A weak passphrase makes the encryption worthless.** A short dictionary
  word can be brute-forced offline from a public repo. Use a long, random
  passphrase (4+ random words) and store it in a password manager.
- **`GITHUB_PAT` is inside this bundle.** It can push to this repository. If
  the passphrase is ever guessed, that token is compromised -- rotate it
  immediately at https://github.com/settings/tokens.
- Anything ever pushed to a public repo must be treated as potentially
  exposed. Changing the passphrase re-encrypts future copies but does not
  erase the old blob from git history.
