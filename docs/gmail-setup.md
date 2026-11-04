# Connecting your Gmail (read-only)

The dashboard can read the e-mails about your applications and move their cards on the Tracker for you: a
confirmation moves a card to Applied, an interview invite to Interview, a rejection to Rejected, and so on. This
guide connects your own Gmail in about ten minutes. You only need a Google account and the dashboard running.

## 1. What it does, and what it never does
- It reads your Gmail **read-only**. It can never send, delete, archive, mark or change any e-mail.
- What it reads: the first sync searches back 60 days (the number is in Settings) for e-mails that may be about
  applications (from job systems such as Greenhouse or Lever, or with words like "application" or "interview" in the
  subject), at most about 5,000 e-mails on that first look back. After that, each sync looks at every new e-mail's
  sender, subject and Gmail's short preview (not its text) to decide whether it is about one of your applications.
- What it skips: your own sent mail, drafts, spam, the bin and chats. Their sender and subject may be looked at, but
  they never move a card and leave nothing behind.
- What it leaves alone: an e-mail counts as about an application only when it comes from a job system, continues a
  conversation already matched to one of your jobs, or uses a job word such as "application", "candidate" or
  "interview". Anything else (personal mail, newsletters, a shop's order or sale mail, even from a company you applied
  to) usually leaves nothing behind in the dashboard. Mail that uses a job word in another sense (a rental or
  credit-card "application", say) may still appear on **E-mails to check**: **Dismiss** removes it and forgets its
  subject.
- When it moves a card: only on a clear e-mail, and only forward. Mixed news (for example an offer that also says
  "we will not be moving forward"), or a soft no without a clear one ("unfortunately", "regret to inform", "not
  selected", "the position has been filled", …), waits on **E-mails to check** for the local model or you.
- What it keeps, only for an e-mail it matched to one of your applications or that waits for you on **E-mails to
  check**: the date, the sender's domain (for example `greenhouse.io`), what it was (confirmation, interview, …) and
  the job. The subject is kept only while the e-mail waits on E-mails to check, and is forgotten once you decide. The
  text of an e-mail is never saved.
- An e-mail's text stays on your computer. When the fixed rules can't tell what an e-mail about an application is,
  only the local model on your own computer (Ollama) reads it, and only if your scoring model in Settings is the
  local one; a cloud model never reads your mail.
- The sign-in it uses is saved only in your computer's keychain, never in a file.

You connect it with your **own** Google Cloud project, so no one else's app ever gets access to your mail.

## 2. Create a Google Cloud project
1. Open [console.cloud.google.com](https://console.cloud.google.com) and sign in with your Gmail account.
2. At the top, open the project list and choose **New project**.
3. Give it any name, for example `auto-apply`, and press **Create**. Make sure the new project is selected at the top.

## 3. Turn on the Gmail API
1. In the menu, open **APIs & Services → Library**.
2. Search for **Gmail API**, open it and press **Enable**.

## 4. Set up the consent screen
Google calls this the "OAuth consent screen" (in newer menus: **Google Auth Platform**).
1. Choose User type **External** and press **Create** (or **Get started**).
2. App name: anything, for example `auto-apply`. Support e-mail and developer contact: your own e-mail address.
3. Under **Data access** (or **Scopes**), add only this one: `.../auth/gmail.readonly` ("View your email messages and
   settings"). Don't add any other.
4. Under **Audience**, leave the publishing status on **Testing**, and add your own Gmail address as a **test user**.
   Only test users can connect while the app is in testing.

## 5. Create the client
1. Open **Clients** (or **APIs & Services → Credentials → Create credentials → OAuth client ID**).
2. Application type: **Desktop app**. Name: anything. Press **Create**.
3. Google shows a **Client ID** (it ends with `.apps.googleusercontent.com`) and a **Client secret**. Keep this window
   open for the next step.

Treat the client secret like a password: don't share it, don't send it to anyone and don't save it in a file.

## 6. Connect in the dashboard
1. Open the dashboard, then **Settings → Gmail**.
2. Paste the Client ID and the Client secret, and press **Save client**.
3. Press **Connect**. Google asks you to choose your account and to allow the app to **read your email**; if Google
   shows a box to tick for that, tick it (without it the dashboard can't read anything and says so). Google may warn
   that the app isn't verified: that's your own app in testing, so choose to continue.
4. You come back to a page saying **Gmail connected**. Press **Continue to Settings → Gmail**: it shows your address
   and the time of the last sync.

The first sync reads the last 60 days; after that the dashboard syncs every 30 minutes while it is open. Both numbers
are in the **Gmail** section of the main **Settings** page. **Sync now** on Settings → Gmail syncs at once.

## 7. Testing mode: connect again about once a week
Google ends a testing app's sign-in after 7 days. Settings → Gmail shows when ("Google asks you to connect again after
7 days, around <date>"). When the sync says **Gmail needs connecting again**, open Settings → Gmail and press
**Connect again**. The next sync carries on from the last sync that worked. If the gap was long and Gmail no longer
has the list of changes since then, the sync searches again from the day before that last good sync, the same way as
the first sync: e-mails from job systems or with job words in the subject, up to about 5,000. A reply such as
"Re: Tuesday" from a company in that gap isn't found that way; check your inbox for anything from the companies you
applied to. E-mails it already read are not read twice.

Taking the project out of testing would need a review by Google; you don't need that for your own use.

## 8. Disconnect
- In the dashboard: **Settings → Gmail → Disconnect**. The dashboard asks Google to forget the sign-in, removes it
  from your keychain, and forgets where the last sync stopped, so connecting again (even another account) starts with
  a fresh look back. The e-mails it already sorted stay on your cards.
- Pressing **Connect again** while connected simply replaces the saved sign-in with the new one. Only Disconnect asks
  Google to forget a sign-in.
- In your Google account: [myaccount.google.com](https://myaccount.google.com) → **Security** → **Your connections to
  third-party apps & services** (or **Third-party access**) → your app → **Delete all connections**. Use this too if
  the dashboard says Google could not be told.

## 9. If something goes wrong
- **"Google refused the sign-in"**: check the Client ID and secret on Settings → Gmail (paste them again), and check
  that your Gmail address is a test user (step 4).
- **"Gmail needs connecting again"**: the 7-day testing limit (step 7), or access was removed in your Google account.
  Press **Connect again**.
- **"The read-only permission wasn't granted"**: on Google's page the box to read your email wasn't ticked. Press
  **Connect** again and tick it.
- **"This sign-in is no longer valid"**: the Connect page was used twice or waited more than 10 minutes. Press
  **Connect** again.
- **Unclear e-mails stay on "E-mails to check"**: the local model (Ollama) is off, a scoring run is using it, or your
  scoring model in Settings is a cloud service (your mail is never sent to one). They are sorted on a later sync, or
  you can decide them yourself.
