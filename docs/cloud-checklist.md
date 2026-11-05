# Checklist for a cloud computer

**No decision has been made to use a cloud computer, and nothing in this file is built.** The app runs on your own
laptop. This is a list of what would have to be true first, should you ever consider one. It doesn't say what one would be
for, and it doesn't recommend one.

Why it needs its own list: today's protection is mostly "it's my laptop, and only I can touch it". On a cloud computer,
someone else owns the machine, it's always on, and it's on the internet.

Facts about a provider below were checked on 2026-11-05 and can change. Check them again.

## What would break
| On the laptop today | On a cloud computer | What it would take |
|---|---|---|
| Your computer's keychain holds the database key, AI keys and the Gmail sign-in | A server has no desktop keychain, so **the app won't start** | One of the keychain options below |
| A browser window you can see | There is no screen, and "you press Submit in the window" isn't possible. Hidden browsers are also what job sites' bot checks look for | Filling forms in a browser, when the app has it, stays on a machine with a screen; a remote desktop would add another exposed service |
| The local AI model runs on your graphics card | A small free machine is far too slow for it | A cloud AI model, which means your resume leaves the computer. E-mails the fixed rules can't sort stay unclear |
| The install lock file is for ordinary 64-bit Linux | A free Arm machine can't use it | A separate lock file, and a check of every package |
| Gmail's sign-in comes back to `127.0.0.1` | The browser isn't on that machine | Sign in once through an SSH tunnel |
| The app opens a browser and asks for passwords in a terminal | There's no browser, and prompts need SSH | Harmless; do the setup over SSH |
| The lock-out after wrong passwords and the logins are kept in memory | An automatic restart clears the lock | Keep the lock across restarts |
| Your home connection looks like a person | Datacentre addresses trip bot checks and rate limits more | Job boards' own data is likely fine, and form pages may not be. How much isn't known |
| Only you are near the data | The provider holds the disk and, in principle, the memory | Encryption limits a stolen disk, **not** a provider with access to the running machine. Accept that plainly or don't do it |
| You start the app by hand | It must start itself after a reboot | A background service, running as an ordinary user |

## Keychain options
| Option | How | Trade-off |
|---|---|---|
| Type a password after each reboot | Unlock a keyring by hand over SSH, or use the master password | The strongest: nothing on the disk unlocks the data. The app is down after every restart until you log in. Reboots are rare, and it's honest about the protection |
| `systemd-creds` | An encrypted credential, unlocked at start by the machine's own key | Starts by itself. A free machine probably has no security chip (not sure), so the key sits on the same disk as the data, which protects little. Needs new code in the app |
| A plain file of environment settings | | **Don't.** It's exactly what the app's keychain rule exists to prevent |

## One provider's facts: Oracle Always Free
Named here only because its free limits are clear enough to check against. Checked 2026-11-05 on Oracle's page, which shows
no date of its own.
- 2 Arm cores and 12 GB in total, 200 GB of storage, five backups, home region only.
- "Out of host capacity" is common enough for Oracle to document it.
- **Idle machines may be reclaimed.** Idle means, over 7 days: 95th-percentile CPU under 20%, network under 20%, and on Arm
  memory under 20%. This app is mostly idle, so it would likely count. Don't fake load: keep **tested backups** so that
  losing the machine costs little.
- A card is requested to prove who you are.

## The checklist
**Reach**
- [ ] No inbound port open to the internet: no public SSH, no port 443. Tailscale suggests UDP 41641 as an optional speed-up;
      leave it closed unless needed.
- [ ] The dashboard is on `127.0.0.1` behind `tailscale serve`, and a Funnel visit is refused when tested from outside.
- [ ] `ss -ltn` shows nothing on public addresses.
- [ ] A firewall set to refuse by default, both the provider's and the machine's own.
- [ ] You know how to use the provider's serial console, the way back in if Tailscale fails.

**Logging in to the machine**
- [ ] Tailscale SSH or SSH keys only; passwords off; no root login.
- [ ] The app runs as its own ordinary user, with no `sudo`.
- [ ] Tailscale key expiry is decided. Tailscale suggests turning it off for servers, and warns of the risk.
- [ ] Tailnet access rules limit who reaches this machine; device approval considered.

**Data and secrets**
- [ ] A keychain option is chosen; nothing secret in environment files, shell history, logs or service files.
- [ ] The data folder is yours only (`0700`), with the database encryption on and the provider's disk encryption on.
- [ ] You've accepted, in writing, that the provider could in principle read the running memory.
- [ ] You've listed what's on it: resume, answers, Gmail sign-in, AI keys. Gmail is read-only.
- [ ] You know which keys to replace if the machine is lost.

**Keeping it healthy**
- [ ] Automatic security updates on; reboots planned, with any password step in mind.
- [ ] Backups are encrypted, kept off the machine, **and a restore has been tested**.
- [ ] Alerts for disk space and failures, with no personal data in them.
- [ ] The lock-out survives restarts; the phone's login cookie and the logout after inactivity are confirmed.

**Only one copy in charge**
- [ ] One way of moving over: stop the first copy, copy the data, run `python -m app.db recover` on the second, confirm. A marker
      in the data folder says which copy is in charge.
- [ ] Filling forms stays on one machine, and what happens to prepared jobs is decided.

**Leaving**
- [ ] A way to delete everything: disks, backups, the Tailscale device, Gmail access, all keys.
- [ ] No charge is possible: only free machine shapes, and a budget alert.

**Before using it:** a fresh, independent review of the running machine, signed off by you.
