# JobRadar

A personal job monitor for Dubai and the UAE. It runs on GitHub Actions every 15 minutes, scores new vacancies against
its owner's profile, sends Telegram alerts, and publishes an **encrypted** dashboard to GitHub Pages.

This repository contains only program code and the public list of employers it watches.
Everything personal is kept elsewhere:

- The profile, CV, Telegram and e-mail settings are GitHub secrets.
- The database and tailored CVs are AES-256-GCM encrypted (`vault.bin`).
- The dashboard on GitHub Pages is ciphertext. It opens only with the owner's private link.

It does not apply to jobs automatically, does not log in to job sites, and polls public job feeds at a low rate.
