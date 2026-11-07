# Filling job forms with the browser plugin

The plugin fills a job's form in your browser with the answers you saved in the dashboard. You check the form and press
Submit yourself: the app never sends an application for you.

## Read this before your first fill
1. **A site's own script sees what is filled.** The moment a box is filled, the site's own script can see it, send it
   and even press its own Submit. The plugin can't stop that. Fill only jobs you would apply to.
2. **Every employer on a site shares one website.** All the employers on Lever share one website, and so do those on
   Greenhouse and on Ashby. Before a fill, close other tabs on that same site. Read the job's name in the plugin's popup
   before you press Fill. If Open and fill leaves your tab empty while the job's box says your answers went to the
   browser, something else took the fill: stop, and look at Settings → Browser plugin.
3. **Load a copy of the plugin, not the project's own folder.** "Install" below says why.
4. **Use a Chrome profile that holds only this plugin.** Any other plugin allowed on job sites can read what is filled.
5. **The key sits unencrypted in your Chrome profile.** A program running as you, or a backup of that folder, can read
   it. It needs a live login to get anything, and pressing Remove in Settings → Browser plugin kills it at once.
6. **Fixes don't arrive by themselves.** A fix to the plugin means replacing your copy and pressing Reload. Keep Chrome
   itself updated too.

## What the app never does
- It never presses Submit, and it never sends an application.
- Nothing fills without your yes for this job: Open and fill in the job's box, or Fill in the plugin's popup.
- It draws no button inside a job's page.
- **Open the posting** only opens the posting. It never fills.
- While it watches the form, it never reads a password box, or a box whose question mentions a password, passcode or
  PIN.
- Passwords, bot checks and the lines under **Left for you** stay yours.

## Install
This takes about ten minutes. Menus in Chrome change, so a name here may differ a little.

1. **Make a separate Chrome profile.** Look for your picture at the top of Chrome, then something like **Add**. Keep
   this profile for the plugin alone.
2. **Copy the plugin to a folder outside the project.** In a terminal, in the project's folder, run:

   ```bash
   mkdir -p "$HOME/auto-apply-plugin" && cp -r plugin/. "$HOME/auto-apply-plugin/"
   ```

   Copy from a version that you, or someone you trust, have read. Chrome runs the files in the folder you give it, and
   a change on disk reaches the plugin with no reload. So a folder you work in would be live code.

   Keep this path for good. Chrome names the plugin by its folder, and your key is tied to that name. If the folder
   moves, the plugin gets a new name and its key is refused.
3. **Load it.** In the new profile, open `chrome://extensions`, turn on **Developer mode**, press **Load unpacked** and
   pick that folder.
4. **Get a key.** In the dashboard, open **Settings → Browser plugin**. Under **Add a browser**, type a name, such as
   Laptop Chrome, and press **Add**. Copy the key: it is shown only once.
5. **Connect.** Press the plugin's icon in Chrome (it may be under the puzzle piece), press **Connect**, paste the key
   and press **Connect** again.
6. **Compare the plugin's ID.** Settings → Browser plugin now shows a Plugin ID. It must match the ID on
   `chrome://extensions`. If it doesn't, remove that browser.
7. **Close Chrome and open it again, once.** The plugin should still be connected.
8. **Check Chrome's site access.** Other plugins must not be able to reach `127.0.0.1` or your phone's address. The
   separate profile takes care of this. If you use a profile with other plugins, set each one's site access by hand.

**To take a fix:** run the same copy command, then press **Reload** on the plugin at `chrome://extensions`.

## Each job
The plugin works in Chrome only, and only with the dashboard on its normal port, 8765.

**Open and fill** is in the job's box, under **Fill the form**. It shows once every decision is answered and a browser
is connected. It opens the form in a new tab and fills it. Reload this page to see how it went: the box doesn't update
by itself. Words you may see in its place:
- **Answer the decisions above first:** some questions are still open.
- **Connect a browser first:** finish "Install".
- **Today's fills are used:** you've reached the day's limit. Wait for tomorrow, or raise the number.
- **The next fill can start in a few minutes:** that's the pause.

Both numbers are in **Settings → Filling forms**. The limit is 10 fills a day and the pause is 2 minutes, unless you
change them. Pressing again on a job whose form no browser has opened yet doesn't count as another fill.

**The toolbar's Fill** does the same for the form in the tab you are looking at. Press the plugin's icon, read the job's
name, and press **Fill**. You must be logged in to the dashboard. Toolbar fills show on the dashboard's first page for 7
days, so you can spot one you didn't start.

Stay on the form's tab while it fills. In a tab behind, a drop-down the plugin left can stay open over the next
question.

**What is left for you:**
- **Left for you** in the job's box lists what you do yourself on the form. The app never answers these.
- **Check these on the form** appears after a fill. It lists what the plugin couldn't set, and what looks different from
  what the app read. On Greenhouse the plugin picks in a drop-down only when exactly one choice has the answer's whole
  words. On other sites, and for a question that starts with a country, place or state, a custom drop-down is yours to
  pick.

**When a fill stops,** the box and the popup say why:
- **The site wants to check you are a person:** finish the check in the tab, then fill in the rest yourself.
- **The site says you've reached its limit:** try this job again later.
- **The page didn't look like this job's form:** check the tab, then press Open and fill again.
- **The form wouldn't let the plugin set any box:** fill it in yourself.
- **This tab was reloaded,** which only the popup says: a reload doesn't fill again, and the form may be empty. Press
  Fill to start over.

**Ashby forms** have to be read first. Press **Read the form** in the job's box. A new tab opens, the plugin reads the
questions, and nothing of yours goes to the site. This doesn't count as a fill. Then reload the job's page, answer what
is open, and use Open and fill. The plugin can't read yes/no buttons, or questions that appear after an answer: do
those yourself on the form.

These questions are what the page showed, and a script on the page can change them: read them before you fill. So apart
from your name, e-mail and resume, under their plain words, each answer waits for you to press Use, or Use all. Use
works for a file too, such as a cover letter: if a newer version is made, you say yes again. If a later read finds fewer
questions, the box says how many went. If it finds under half of them, the saved form stays, and the popup says so.

## After you press Submit
- **It looks sent. If the site took it, press I submitted it.** The plugin noticed a press of one of the form's own
  submit buttons. Any of them counts, even one with other words, such as Save draft. Nothing is recorded yet, because
  the site may have refused the form.
- **Some sites' Submit buttons aren't noticed.** On Ashby nothing is noticed yet: it has no form to go by, until a real
  application there has shown which button is its Submit. **I submitted it** works all the same.
- **The popup** says only that you pressed Submit, as it can't see the job's box.
- **I submitted it** saves the record and moves the card to Applied. You choose the date.
- **I didn't send it** ends the fill and wipes what the plugin read. Nothing is recorded and no card moves. It still
  counts towards the day's fills, because your answers did go to the page.
- **Closing the tab** records nothing. You can still say what happened afterwards, in the job's box.

**Recording by itself** is off for every site. A site can be switched on only after a first real application there has
shown what its thank-you page looks like. Once on, the app records by itself if that page follows your own press of
Submit within 2 minutes, while you are logged in.

Be careful with this one. A wrong automatic record can't be erased: you can only mark it by moving the card back. A
page can fake a thank-you page after your real press, and a page can also make a click of yours count as that press. So
read the form and the site's own answer before you press **I submitted it**.

## What the record means
- **What the form held when last checked** is what the form showed the plugin. It isn't proof of what the site received.
- The plugin reads the form every 5 seconds or so, so a change in the last few seconds may be missed.
- Only questions the app knew about are there. A new question you answered on the form isn't.
- The record names the PDF the app handed out. It says the box showed another file only if its name differs. That other
  name is never kept, as it may hold a person's name.
- With no read to go on, the record is by hand: **What the app prepared (you applied by hand)**.

## One thing nobody can prevent
A page's own script can act on a change, and that includes ticking a radio button or a box. Any script on one of these
sites can reach every form on that site. What the plugin reads back comes through the page, so a page can show it
something false.

## Other things to know
- After a Submit, filled values go into Chrome's form history, the same as typing by hand.
- Whatever answers on port 8765 gets the key. So a program that takes that port while the dashboard is off would hear
  it. That needs a program already on your laptop, and such a program can usually read the key anyway.
- Someone with the key and a live login could reach the answers of up to the day's limit of jobs each day, until you
  press Remove.

**If something looks wrong,** go to Settings → Browser plugin and press **Remove** on that browser. If you aren't sure
which one, remove them all. If your phone is set up, **Log out all devices** in Settings → Phone does that too, and each
browser then needs a new key.
