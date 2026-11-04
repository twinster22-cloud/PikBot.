# PikBot Neon membership setup

Set these environment variables on a staging service before deploying this branch.

- `DATABASE_URL` points to persistent PostgreSQL. The app creates the `pikbot_accounts` table at startup.
- `PIKBOT_SESSION_SECRET` is a stable, randomly generated signing secret.
- `STRIPE_SECRET_KEY` is a Stripe secret key for the intended mode.
- `STRIPE_PRICE_ID` is an active recurring $15 USD monthly price in the same Stripe account and mode.
- `PIKBOT_PUBLIC_URL` is the HTTPS origin of the deployed app.

Enable the Stripe customer portal with cancellation settings. Use Stripe test mode to verify checkout, return, portal access, and cancellation before using live credentials. Checkout charges immediately when the user explicitly subscribes. The independent seven-day trial begins at registration and requires no card.

Before public sign-ups, add email verification and password recovery. This branch does not connect live player props.
