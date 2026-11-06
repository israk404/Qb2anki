# Security

## How the app protects you

- The app's server listens only on `127.0.0.1` (your own computer) and checks the `Host` header.
- Every API request needs a random token created at each start, so other websites open in your browser cannot control the app.
- File downloads and pictures are served only from the app's own `work` and `out` folders.
- API keys live in `.env` and `keys.txt` on your computer. They are never written to the database, the log or the debug bundle,
  and the app shows at most their last four characters outside the Settings page.

## Keeping your keys safe

- Never commit `.env`, `keys.txt` or `settings.json`; they are listed in `.gitignore`.
- Do not share your app folder or upload it anywhere public.
- If a key is exposed, delete it at <https://aistudio.google.com/apikey> and create a new one.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
[private vulnerability reporting](https://github.com/israk404/qb2anki/security/advisories/new) for this repository instead.
