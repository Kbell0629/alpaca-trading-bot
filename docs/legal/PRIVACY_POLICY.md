# Privacy Policy

**Last updated: [EFFECTIVE_DATE]**

**[COMPANY_NAME]** ("we", "our", "us") operates the trading automation software and related services (the "Service") at **[DOMAIN]**. This Privacy Policy explains what information we collect, why we collect it, and how we handle it.

We built this Service with privacy-by-default: we collect only what we need to operate the Service, we never sell your data, and we give you control over deleting it at any time.

---

## 1. Information We Collect

### 1.1 Information you provide

- **Account information:** email address, username, hashed password
- **Brokerage API credentials:** API key and secret to your Alpaca (or other supported broker) account. Stored encrypted at rest using AES-256-GCM.
- **Configuration preferences:** strategy presets, kill-switch thresholds, notification topic, notification email
- **Payment information:** handled by Stripe, Inc. We never see or store your full card number. We store only the Stripe customer ID and subscription status.
- **Support communications:** emails and in-app messages you send to our support channels

### 1.2 Information we collect automatically

- **Trade data:** every order the Service places or observes in your brokerage account (symbol, qty, price, timestamp, strategy, outcome). Used to render your dashboard, compute the scorecard, and train the self-learning weights.
- **Usage analytics:** page views, feature clicks, session duration, API endpoints hit. We use this to identify bugs and improve the product.
- **Technical data:** IP address (used for rate limiting and security), browser user-agent, OS type, timestamps of logins.
- **Error data:** stack traces of exceptions that occur while you're using the Service. Sent to Sentry for debugging; stripped of user-identifying data.
- **Audit logs:** every admin action, login attempt, and sensitive operation (kill switch toggle, factor bypass, backup download). Retained 90 days for security forensics.

### 1.3 Information we DO NOT collect

- We do **not** collect your Social Security Number, tax ID, or government ID
- We do **not** collect banking or credit card numbers directly (Stripe handles this)
- We do **not** collect your bank account balance or net worth
- We do **not** collect data from your device unrelated to the Service (contacts, location, photos, etc.)
- We do **not** access your brokerage account for any purpose other than executing trades you configured

## 2. How We Use Your Information

We use collected information to:

- **Operate the Service:** authenticate you, execute trades via your broker, display your dashboard, send notifications
- **Provide customer support:** respond to questions and troubleshoot issues
- **Improve the Service:** identify bugs, measure feature usage, improve algorithms
- **Security:** detect unauthorized access attempts, enforce rate limits, investigate fraud
- **Compliance:** meet legal obligations (tax reporting requirements, responses to lawful subpoenas)
- **Communications:** send transactional emails (trade notifications, billing receipts, security alerts) and — only with your opt-in — product announcements

**We do not use your data to train third-party AI models.** The Service uses LLM APIs (Google Gemini, OpenAI, etc.) for news sentiment analysis, but we send only **public news headlines and ticker symbols** — never your personal trading history or account data — and we configure each API with `send_default_pii=false` equivalents where available.

## 3. How We Store and Protect Your Information

### 3.1 Encryption

- **In transit:** all communication uses TLS (HTTPS). Railway edge termination provides encrypted transport.
- **At rest:**
  - Brokerage API credentials: encrypted with AES-256-GCM using per-deployment keys
  - Passwords: hashed with PBKDF2-SHA256 (600,000 iterations, OWASP 2023 recommendation)
  - Session tokens: random 48-byte tokens stored in database
  - Database: SQLite with WAL mode; file permissions enforced to 0600

### 3.2 Infrastructure

The Service runs on **Railway.app** (primary) with optional off-Railway backups to S3, Backblaze B2, or GitHub releases (your choice at deploy time). Data is stored in US data centers. Railway employees cannot read your encrypted credentials without the encryption key we hold.

### 3.3 Access Control

Only [COMPANY_NAME] employees with a need to access user data can do so, and only for support, security, or legal compliance purposes. All such access is logged.

### 3.4 Breach Notification

If we discover a data breach affecting your information, we will notify you via email within **72 hours** of confirmation (matching GDPR Article 33 timing), describing what was exposed and what we've done.

## 4. Third-Party Service Providers (Subprocessors)

We use the following third parties to provide the Service. Each is bound by a data-processing agreement where applicable.

| Subprocessor | Purpose | Data shared |
|---|---|---|
| Railway.app | Hosting | All application data (encrypted) |
| Alpaca Securities LLC | Trade execution | Your Alpaca API credentials, order instructions |
| Stripe, Inc. | Payment processing | Name, email, card info (they handle directly) |
| Yahoo Finance (yfinance) | Market data | Ticker symbols (no user data) |
| SEC EDGAR | Insider filings | Ticker symbols (no user data) |
| Google Gemini | News sentiment analysis | News headlines + ticker symbols (no user data) |
| OpenAI / Anthropic / Groq | Alternative LLMs for sentiment (optional) | News headlines + ticker symbols |
| Sentry | Error tracking | Stack traces (user identifiers scrubbed) |
| ntfy.sh | Push notifications | Notification topic + message body |
| Gmail SMTP | Email delivery | Your email, message subject + body |
| UptimeRobot | Health monitoring | Public /healthz endpoint polls (no user data) |

## 5. Data Retention

We retain data as follows:

| Category | Retention |
|---|---|
| Account + credentials | Until you delete your account + 30 days for backup rotation |
| Trade history | Until you delete your account (you can export anytime) |
| Usage analytics | 1 year |
| Error logs (Sentry) | 90 days |
| Audit logs | 90 days |
| Daily backups (encrypted) | 14 days rolling |
| Billing records | 7 years (IRS requirement) |

You can request deletion of data earlier by emailing [PRIVACY_EMAIL]. We will comply within 30 days unless legally required to retain it.

## 6. Your Privacy Rights

### 6.1 All users

- **Access:** download a copy of your data via Settings → Export (JSON format)
- **Correction:** update incorrect info via Settings
- **Deletion:** delete your account via Settings. Credentials removed immediately; backups purged within 14 days.
- **Portability:** your exported data is JSON — importable into most tools

### 6.2 California residents (CCPA)

You have additional rights under the California Consumer Privacy Act:

- Right to know what personal information we collect (this Privacy Policy)
- Right to delete personal information (Settings → Delete Account)
- Right to opt out of the sale of personal information — **we do not sell personal information**
- Right to non-discrimination for exercising your rights

To exercise CCPA rights, email [PRIVACY_EMAIL] with "CCPA Request" in the subject.

### 6.3 EU / EEA / UK residents (GDPR)

You have additional rights under the General Data Protection Regulation:

- Right of access (Article 15)
- Right to rectification (Article 16)
- Right to erasure ("right to be forgotten", Article 17)
- Right to restrict processing (Article 18)
- Right to data portability (Article 20)
- Right to object to processing (Article 21)
- Right to lodge a complaint with your local data protection authority

**Legal basis for processing:** performance of contract (most features), consent (marketing emails), legal obligation (tax/KYC if required), legitimate interest (security, fraud prevention).

**International transfers:** your data is stored in the United States. For EU residents, we rely on Standard Contractual Clauses (SCCs) with our US subprocessors where applicable.

## 7. Cookies and Tracking

### 7.1 Cookies we use

- **Session cookie** (required): keeps you logged in. HttpOnly, SameSite=Strict, Secure in production. Expires after 30 days of inactivity.
- **CSRF cookie** (required): double-submit cookie for Cross-Site Request Forgery protection.

### 7.2 Cookies we do NOT use

- We do **not** use third-party analytics (no Google Analytics, no Facebook Pixel, no ad trackers)
- We do **not** use advertising cookies
- We do **not** cross-domain track

Because we only use essential cookies, we don't need a cookie consent banner under GDPR's strictest interpretation. You can disable cookies in your browser, but the Service will not function without the session cookie.

## 8. Children's Privacy

The Service is not directed to children under 18. We do not knowingly collect personal information from anyone under 18. If we learn that we have collected data from a minor, we will delete it promptly. If you believe a minor has submitted information, email [PRIVACY_EMAIL].

## 9. Changes to This Privacy Policy

We may update this Privacy Policy from time to time. For material changes (new types of data collected, new subprocessors with broader access, changes to retention periods), we will notify you by email at least **30 days before** the changes take effect. Continued use after the effective date constitutes acceptance.

Non-material changes (typos, clarifications, new subprocessor for an existing category) will be reflected with an updated "Last updated" date above.

## 10. Do Not Sell or Share (CCPA)

**We do not sell or share personal information as defined under the CCPA/CPRA.** We do not rent or lease your data to third parties for marketing purposes, and we do not participate in cross-context behavioral advertising.

## 11. Notice to Brokers and Data Providers

Alpaca Securities LLC and other brokers have their own privacy policies governing your brokerage account. Our Privacy Policy covers only data we collect about your use of the Service. When you place a trade via the Service, the trade details also flow through your broker's privacy policy.

## 12. Contact

Questions, requests, or complaints about privacy:

- **Email:** [PRIVACY_EMAIL]
- **Mail:** [COMPANY_ADDRESS]

For EU/EEA residents, our Data Protection Officer (or equivalent contact) can be reached at the same address.

---

**Summary for the impatient:**

- We collect only what's needed to run the bot (email, encrypted broker credentials, trade data)
- We never sell your data
- You can delete everything at any time
- All data is encrypted at rest and in transit
- US-based infrastructure (Railway)
- No ad trackers, no analytics tracking across sites
- Compliant with GDPR and CCPA
