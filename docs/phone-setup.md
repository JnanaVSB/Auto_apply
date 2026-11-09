# Using the dashboard on your phone

The dashboard stays on your laptop, and your phone reaches it through your own private Tailscale network. Your job data
isn't kept on the phone, and nothing outside that network can reach the dashboard. This guide takes about twenty minutes.

Nothing in this guide can be done by the app for you: the account, the sign-ins and the commands are yours.

**How sure this guide is:** it was written from Tailscale's documentation and source code and tested with made-up
requests, not yet on any real phone. Menus in Tailscale and on phones change. Where something isn't known, it says so.
The last section is a short test to run on your own phone.

## Read this first: your laptop's name becomes public
Turning on HTTPS in Tailscale gets a real certificate for your laptop. Every such certificate is written into a public
log that anyone can read and **nobody can erase**, and it holds your laptop's full name, such as
`laptop.tail0000.ts.net`.

So, before you turn on HTTPS:
1. If your laptop's name holds your real name or anything personal, rename it first. In Tailscale's admin page, look for
   something like **Machines**, then the laptop's menu, then **Edit machine name**.
2. Choose a randomised tailnet name (the `tail0000` part) rather than one that says who you are. Look for something like
   the admin page's **DNS** page.

If you've already turned on HTTPS, the old name stays in the log, even after you rename the laptop.

## 1. Set it up
1. Make a free Tailscale account at [tailscale.com](https://tailscale.com). The free Personal plan was up to 6 users and
   unlimited devices when checked on 2026-11-05.
2. On something like the admin page's **DNS** page, turn on **MagicDNS** and **HTTPS Certificates**.
3. Install Tailscale on the laptop and on the phone, and sign in to the same account on both.
4. On Linux only, run this once, so that the next command works without `sudo`:

   ```bash
   sudo tailscale set --operator=$USER
   ```

5. Run this exact command on the laptop. **Use it exactly as it is, and never change it** (section 3 says why):

   ```bash
   tailscale serve --bg 8765
   ```

   It keeps working after a restart of the laptop, so you only run it once.
6. Check it:

   ```bash
   tailscale serve status
   ```

   It must show an `https://` address sending to `http://127.0.0.1:8765`, for example
   `https://laptop.tail0000.ts.net` sending to `http://127.0.0.1:8765`. If it shows anything else, run
   `tailscale serve reset` and do step 5 again.
7. In the dashboard on the laptop, open **Settings → Phone**, paste that `https://` address, and press **Save**.
8. Restart the dashboard: press Ctrl+C in its terminal, then start it again without `--port`:

   ```bash
   python -m app.web
   ```

9. On the phone, with Tailscale on, open the `https://` address and log in with your dashboard password.
10. Install it:
    - **Android:** in Chrome, open the menu and press **Install app**.
    - **iPhone:** press **Share**, then **Add to Home Screen**.

## 2. What stays out
| Who | What happens |
|---|---|
| Anyone on the same Wi-Fi | Nothing to reach: the dashboard listens only on the laptop itself |
| The public internet, even if Funnel gets switched on | The dashboard refuses it, before the login page |
| Another website open in your phone's browser | Refused, as on the laptop |
| Any other device in your tailnet | It can open the login page, so it needs your password |

You can see the first one yourself. On Linux, run:

```bash
ss -ltn
```

Look at the lines with `8765`. The only one must say `127.0.0.1:8765`. If you see `0.0.0.0:8765`, `*:8765` or `[::]:8765`,
something is wrong: stop the dashboard. On a Mac, use:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

## 3. Never do these
- **Never run `tailscale funnel`.** It opens the dashboard to the whole internet. The dashboard refuses those visits,
  but there is no reason to send them.
- **Never use `--tcp`, `--tls-terminated-tcp` or `--http`**, with `tailscale serve` or `tailscale funnel`.
- **Never put `socat`, `ssh -L`, `ngrok`, `cloudflared` or the like in front of the dashboard.** These, like `--tcp`, pass
  the connection on without the marks the dashboard relies on, so only your password would protect it.
- **Never share Ollama** through Tailscale. It has no password.
- **Keep your tailnet to your own devices.** Every device in it can reach the login page, so don't use a school or work
  tailnet, or share this one with other people.

`tailscale funnel reset` is what turns Funnel off, but it also removes the phone's sharing. If you ever run it, run
`tailscale serve --bg 8765` again afterwards.

## 4. What the phone can and can't do
On the phone you can read your jobs, decide on them, use the Tracker, add a job or a company, and answer E-mails to check.
On Android, you can also **Share** a job link from another app into auto_apply: it fills in the link on Add, and nothing is
saved until you press **Add job**.

Some things stay on the laptop:
- **Connecting Gmail.** Google sends you back to the laptop's own address, so Connect is refused on the phone. Sync,
  Disconnect and the rest of Settings → Gmail work.
- **Changing the phone address.** The phone shows it but can't change it.
- **Filling a form with the browser plugin** happens on the laptop.
  On the phone, press **Do this on a computer** on a job: Ready to Apply lists it under To do on a computer until you
  record it as submitted on the laptop, move its Tracker card on from Ready, or take it off.

On a phone, a long Tracker lane comes 100 cards at a time, and the "Which job" list on E-mails to check opens when you press
it.

## 5. Limits
- The laptop must be on, awake and running the dashboard, and Tailscale must be on, on both devices.
- Offline, the phone shows a plain browser error: your job data isn't kept on the phone.
- If the signal drops while "Which job" loads, reload the page.
- You're logged out after 30 minutes without use, and after 12 hours at most, as on the laptop. A saved phone address
  applies only after the next restart of the dashboard.

## 6. If something goes wrong
- **The phone shows 502.** The dashboard isn't running, or it was started with a different `--port`. Start it with
  `python -m app.web`.
- **The phone shows a browser error and no page.** Check that Tailscale is on, on both devices, and that the laptop is awake.
- **"This is a Tailscale address the dashboard doesn't know."** The address isn't the one saved, or the dashboard hasn't
  restarted since. On the laptop, check **Settings → Phone**, then restart the dashboard. It must match exactly, port
  included.
- **"Open this from a phone signed in to your Tailscale."** The visit arrived without your Tailscale sign-in. Check the phone
  is signed in to your own account.
- **"The dashboard can't be opened from the public internet."** Funnel is on. On the laptop, run `tailscale funnel reset`,
  then `tailscale serve --bg 8765`.
- **The dashboard won't start and names `phone_address`.** The address was typed by hand into `data/config.yaml` and isn't a
  Tailscale address. Correct it, or delete the line.
- **Login is locked.** Five wrong passwords in a row lock login for 15 minutes, on the phone and the laptop alike.
- **Forgot the password.** On the laptop, stop the dashboard and run `python -m app.web reset-password`.
- **The certificate fails.** Tailscale gets certificates from Let's Encrypt, whose limit can mean a wait of 34 hours,
  according to Tailscale's documentation. Nothing is wrong with the dashboard, and the laptop keeps working.

On the laptop, always use the `127.0.0.1` address the terminal shows, not the `https://` one. What the `https://` address
does on the laptop itself isn't known yet.

## 7. If you lose your phone
1. In Tailscale's admin page, look for something like **Machines** and remove the phone. Do this first: it cuts the phone
   off.
2. On the laptop, open **Settings → Phone** and press **Log out all devices**. You'll log in again on the laptop too.
   It also removes every browser in **Settings → Browser plugin**, so each one needs a new key.

## 8. What Android may tell Google
When Chrome installs the app on Android, it may send the app's name and address to Google to build it. This comes from
older reports, and we're **not sure** it's still true. If it can't, Android may add a plain shortcut instead. That address is
public anyway, as the first section explains.

## 9. Undoing it all
1. On the laptop, run:

   ```bash
   tailscale serve reset
   ```

2. Open **Settings → Phone**, empty the address, and press **Save**.
3. Restart the dashboard.

The laptop's name stays in the public certificate log.

## 10. First test on your phone
Do this on each phone, and note anything that isn't right:
- [ ] It installs, and its icon and name look right.
- [ ] The top of the screen isn't hidden.
- [ ] You can log in, and open Ready, the Tracker and Add.
- [ ] A PDF opens, and you can get back.
- [ ] A change saves, with no "page expired".
- [ ] Close the app and reopen it.
- [ ] A wrong password counts.
- [ ] With Tailscale off on the phone, you see a browser error, not the dashboard.
- [ ] `ss -ltn` on the laptop shows only `127.0.0.1:8765`.
- [ ] On Android, Share a job from a real job app: the link arrives on Add.
- [ ] On iPhone, if you can: Safari with VoiceOver, and an older iPhone.
