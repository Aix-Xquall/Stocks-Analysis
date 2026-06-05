# Morning LINE Stock Digest

This repository sends a LINE stock digest from GitHub Actions every Monday to Saturday at 07:00 Asia/Taipei.

## What It Sends

1. A US stock overview image for `NVDA`, `TSM`, `MU`, `GOOGL`, `AVGO`, `ORCL`, `TSLA`, `META`, `MRVL`, `RKLB`, and `VCX` (Fundrise Innovation).
2. A text digest listing only the last two days of ESP/EPS, revenue, outlook, guidance, earnings, or investor conference news for the original US news watchlist plus `2330.TW`, `2454.TW`, `2308.TW`, `8299.TWO`, `2408.TW`, `3260.TWO`, `2368.TW`, and `2327.TW`. Tickers with no matching recent news are omitted.

The script checks LINE monthly usage before sending. If the current month is already at `198 / 200`, or sending two more messages would exceed `198`, it stops and waits for LINE's monthly counter to reset.

## GitHub Setup

Create a GitHub repository and push these files. Then add these repository secrets:

- `LINE_CHANNEL_ACCESS_TOKEN`: your LINE Messaging API channel access token
- `LINE_USER_ID`: your LINE Messaging API recipient user ID, such as `U...`

The workflow schedule is `0 23 * * 0-5` because GitHub Actions cron is UTC. That is 07:00 Monday to Saturday in Asia/Taipei.

## Image Delivery

LINE image messages need a public HTTPS image URL. By default, the workflow commits `public/us_stock_overview.png` back to the repository and sends the raw GitHub URL.

This works best when the GitHub repository is public. If the repository is private, LINE may not be able to fetch the raw image. In that case, change this workflow variable to use a LINE Flex visual card instead of an image:

```yaml
LINE_USE_RAW_GITHUB_IMAGE_URL: "false"
```

## Manual Test

After setting the secrets, open the workflow in GitHub Actions and run `workflow_dispatch`. The run log will show quota checks, quote collection status, and LINE send status without printing the token.
