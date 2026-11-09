# Scoring jobs with a cloud AI model

The app gives every job that passes the quick check an AI score from 0 to 100. By default this runs on your own
computer with Ollama: it's free, and nothing leaves the computer. **Settings → Scoring model** lets you send the scoring
to a cloud service instead: OpenAI, Anthropic, or your own address (any service that accepts OpenAI-style requests,
such as Groq, Cloudflare or OpenRouter). This guide explains what that sends, what it costs, and how to go back.

Nothing here happens by itself. Scoring only leaves your computer after you choose a service and tick its box.

## 1. What leaves your computer
For each job, the cloud service gets your resume and the job ad. The tick-box on the page says exactly what goes:

> Send my resume to *(the service's address)* to score jobs. My name, e-mail, phone number, links and street address
> are replaced with placeholders first. **Everything else in my resume still leaves this computer: my employers,
> projects, schools, dates and skills.** Each job ad goes too. *(The service)*'s own terms decide what it keeps and
> whether it trains on it, so I have read them.

What that means in practice:
- Your contact details become placeholders such as `[my name]`, `[my email]` and `[github link]`. Any other link left
  in your resume, such as a project's, becomes `[other link]`.
- The app needs your full name for this, so fill in your Questionnaire first. Without it, nothing is sent.
- Before anything is sent, a last check looks for contact details that are still there. If it finds any, the run stops,
  nothing is sent, and it tells you what to remove.
- Your e-mails never go to a cloud model. When scoring runs in the cloud, the Gmail e-mails the app's fixed rules can't
  sort stay on **E-mails to check** for you to sort by hand.

## 2. The tick-box
Ticking the box means you've read the service's terms and accept that your resume (contact details hidden) and the job
ads go there.
- It is never ticked for you.
- It's asked again whenever you change to another address or service. Changing only the model at the same address
  doesn't ask again.
- **Score on this computer** clears it. Going back to a cloud service asks again.
- Scoring checks it too: if the address you score with isn't the one you ticked, scoring stops before anything is sent.

## 3. Choosing a service
Read the service's terms before you tick. Look for three things:
- whether it trains its models on what you send;
- what it keeps, and for how long;
- whether it offers zero data retention (keeping nothing).

The app can't check any of this for you. The page says: "Use only a service whose terms say it doesn't train on what
you send."

The page has three buttons, **Groq**, **Cloudflare** and **OpenRouter**. They only fill in the address: you still make
your own account and key, and **none of them is a recommendation**. What the page says about each was read from the
services' public pages on 2026-11-07, and it can change:
- **Groq:** Groq's terms say its services are "not for consumer use"; read them and decide whether that fits a personal
  tool. Groq says it keeps nothing by default, and that customers can switch on zero data retention under Data
  Controls. Its page doesn't say whether that includes the free plan: switch it on, and check that it stays on.
- **Cloudflare:** put your own Cloudflare account id where the address says `YOUR-ACCOUNT-ID` (the app refuses to save
  it until you do). Cloudflare's model names start with `@`, such as `@cf/...`.
- **OpenRouter:** with every request, the app asks OpenRouter to use only hosts that keep nothing and don't collect your
  data. If no host of your model agrees, OpenRouter refuses the request and the app says so. Model names starting with
  `openrouter/` (such as `openrouter/free`) are refused, because OpenRouter would pick the model and host for you.

OpenAI and Anthropic have their own sections on the page, each with a list of models to choose from.

## 4. Keys
- **Your own address:** if it needs a key, tick "This address needs a key" and paste the key in **Your own address's
  key**. It's kept in your computer's keychain under the name `own-scoring`, never shown again and never written to a
  file. You can change it on the laptop only, not from your phone.
- **OpenAI and Anthropic:** these use the same key as the Writer page (**Settings → Writer**). The Scoring model page
  only shows whether that key is saved, with a link to add it.

## 5. Money
- OpenAI and Anthropic count towards your spending limits (**Settings → Writer**). When a request would pass today's
  or this month's limit, scoring pauses until tomorrow or next month (or until you raise the limit).
- Your own address counts only if you give both of its prices (per million tokens read and written). Without prices,
  its costs aren't counted, so the limits can't protect you.
- The top bar shows where scoring goes: "Scoring: laptop, free", or the service's address followed by "counted in your
  limits" or "not counted in your limits".

What one scored job costs, roughly, from the app's price list (checked 2026-10-30, at the normal price). A job sends
about 2,300 tokens and gets about 150 back:

| Model | About, per scored job |
|---|---|
| Claude Haiku 5.5 | $0.0003 |
| GPT-6.1 Terra | $0.003 |
| Claude Sonnet 5.5 or GPT-6.1 Sol | $0.006 |
| Claude Opus 5.5 | $0.012 |

A long resume or ad costs more. The Usage page shows what scoring really cost.

## 6. When a service says "too many requests"
Free plans have daily limits. When a service says too many requests, the app waits as long as the service asks, up to
2 minutes (1 minute if it doesn't say), at most 3 times for one job. A longer wait, or no time given twice in a row, is
taken as the day's limit:
- that job stays waiting, and scoring continues tomorrow;
- automatic scoring waits until midnight on your computer's clock, even if you restart the dashboard;
- **Score these** on the Jobs page can still try.

## 7. Why not a free council too?
Scoring is one short request per job. The council's Writer and reviewers send long texts several times for every job,
so free limits run out quickly, and a free service's terms rarely fit that kind of use.

## 8. Checking that it works
- The **Test** button on the Scoring model page is free: it only asks whether the model can be used, and sends nothing
  of yours.
- In a terminal, `python -m app.score setup` downloads the quick-check files, checks the tick-box, then runs the same
  free check:

  ```bash
  python -m app.score setup
  ```

- Some addresses don't list their models (Cloudflare's documentation shows no list). Then the check says the address
  answers but "your first use will show whether it works", and setup ends as not ready.

## 9. Editing the settings file by hand
The page writes everything for you. If you edit `data/config.yaml` yourself, a model name starting with `@` must be in
quotes, for example:

```yaml
model: '@cf/meta/llama-3.1-8b-instruct'
```

## 10. Going back
Open **Settings → Scoring model** and press **Score on this computer**. That also clears the tick-box. Earlier scores
stay saved under the old model's name, and jobs are scored again with the model you switched to.
