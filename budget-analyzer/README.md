# Personal Budget Analyzer

A Claude Code project that categorizes bank transactions, finds spending patterns and builds a budget from actual spending. The analysis rules live in [CLAUDE.md](CLAUDE.md).

## Usage

1. Export your transactions from your bank (CSV preferred) and save them in `data/`.
   Remove account numbers and other sensitive details first if you prefer.
2. Start Claude Code from this folder (`budget-analyzer/`) so it loads `CLAUDE.md`.
3. Ask, for example: "Analyze my spending for October" or "Audit my subscriptions".

## CSV format

See [`data/sample_transactions.csv`](data/sample_transactions.csv). Required columns: `Date`, `Description`, `Amount`; `Category` is optional.
Use negative amounts for expenses and positive amounts for income.

## Privacy

`.gitignore` keeps everything in `data/` (except the sample) and `reports/` out of git, so real statements are not committed by accident.
