# Legal Documents — Overview & Lawyer-Review Checklist

**Status:** DRAFTS only. Not yet reviewed by a lawyer. Do NOT publish on a public site until reviewed.

## What's in this folder

| Document | Purpose | When you need it |
|---|---|---|
| `TERMS_OF_SERVICE.md` | Rules users agree to when using the bot | Before accepting any paid user |
| `PRIVACY_POLICY.md` | How you handle user data | Required by GDPR + CCPA + Alpaca ToS |
| `DISCLAIMER.md` | Trading-specific disclaimer | Critical — keeps you out of SEC territory |

## Why these matter

The line between "software vendor" (no SEC registration needed) and "investment advisor" (expensive SEC registration) is **how you market + how users interact with the bot**. These documents establish you as a software vendor:

- You sell a tool; users provide their own broker credentials
- You never hold or touch user money
- You never make personalized buy/sell recommendations
- You disclaim everything prominently

Published and signed-off docs are the #1 defense in any dispute (customer claiming they lost money, regulator asking questions, etc.).

## What the drafts need from a lawyer

Budget **$500-2,000 one-time** for a business lawyer to review. Here's the specific list to send them:

### 1. Terms of Service
- [ ] Does the "no investment advice" language hold up under SEC § 202(a)(11) definition of investment advisor?
- [ ] Is the limitation of liability clause enforceable in our jurisdiction (recommend capping at 12 months of fees paid)?
- [ ] Arbitration clause — is AAA or JAMS better for our situation?
- [ ] Class-action waiver enforceability in the user's state
- [ ] Indemnification — is the scope too broad/too narrow?

### 2. Privacy Policy
- [ ] GDPR compliance (Europe users) — right-to-erasure, data portability, legal basis for processing
- [ ] CCPA compliance (California) — categories of data + user rights
- [ ] Children's privacy (COPPA) — we say 13+ only; confirm this is enforceable
- [ ] Alpaca API credentials encryption — does our disclosure match our actual practice?
- [ ] Third-party processors list (Railway, Alpaca, Sentry, yfinance via Yahoo, Gemini API) — is our subprocessor language sufficient?

### 3. Disclaimer
- [ ] Is the "software only, not advice" framing strong enough to avoid accidental investment advisor classification?
- [ ] Past performance / hypothetical results language — matches SEC Marketing Rule (17 CFR § 275.206(4)-1) for performance claims?
- [ ] Risk warnings — sufficient for day trading / options wheel strategy / leveraged ETFs?

## Publishing checklist (AFTER lawyer review)

- [ ] Replace all `[COMPANY_NAME]` / `[STATE]` / `[EFFECTIVE_DATE]` placeholders with real values
- [ ] Host each doc at public URLs:
  - `https://yourdomain.com/terms`
  - `https://yourdomain.com/privacy`
  - `https://yourdomain.com/disclaimer`
- [ ] Link to all three in dashboard footer
- [ ] Add "By signing up, you agree to our Terms, Privacy, and Disclaimer" checkbox on signup flow (checkbox must be unchecked by default — state law requires affirmative consent)
- [ ] Store version + timestamp of TOS each user accepted (schema update)
- [ ] Email current users when TOS updates: 30-day notice before new terms apply

## Business formation checklist (do BEFORE Phase 3 public launch)

- [ ] Form LLC ($200-500 via LegalZoom or Stripe Atlas)
- [ ] Get EIN from IRS (free, 10 min online)
- [ ] Open business bank account
- [ ] Register for sales tax in your state (if applicable — some states don't tax SaaS)
- [ ] Get business insurance — specifically **errors & omissions** and **cyber liability**
  - Hiscox offers SaaS-specific E&O starting ~$500/year
  - Expected annual cost at small scale: $1000-2500

## When the drafts DON'T need lawyer review

Phase 1 (self-trading) and Phase 2 (a handful of friends using informally) — you don't technically need published legal docs. You're not selling to the public.

**But:** publish the disclaimer anyway. One friend losing money and blaming your bot is enough to make you wish you had the disclaimer in writing.

## Recommended lawyer types

- **Business attorney** who has written SaaS Terms of Service before — ideal
- **Securities attorney** — overkill unless you're planning to touch actual investment advice
- **Fintech-focused firm** — sweet spot; understands both sides

Ask for a **flat-fee ToS review** — tell them it's a 3-document package, should take 2-4 hours, target $500-1500. Most business lawyers offer this.

## Rough cost summary

| Item | One-time | Ongoing |
|---|---|---|
| LLC formation | $200-500 | — |
| Lawyer review of 3 docs | $500-2,000 | — |
| Business insurance (E&O + cyber) | — | $1,000-2,500/year |
| Registered agent (for LLC) | $100-300/year | — |

**Total first-year legal/business setup: $2,000-4,500.** Budget $3K to be safe.

## Status log

| Date | Event |
|---|---|
| 2026-04-19 | Initial drafts written by Claude (not a lawyer) |
| TBD | Lawyer review — book appointment when ready for Phase 2 or 3 |
| TBD | LLC formed |
| TBD | Published to public URLs |
