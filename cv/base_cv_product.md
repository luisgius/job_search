# LUIS GIMENEZ

lgimenezuso@gmail.com | Kraków, Poland | Open to relocation (Europe, Singapore, Taiwan) | LinkedIn: linkedin.com/in/luis-gimenez-uso | GitHub: github.com/luisgius

## PROFESSIONAL SUMMARY

Data Scientist with 1.5 years of experience turning operational and commercial questions into decisions: causal analysis of a pricing-model change and forecasting with quantified uncertainty at Uber, and price-response simulation at Buynomics. Works in Python and SQL and explains results to business stakeholders.

## SKILLS

- **Modelling & Inference:** Forecasting (LightGBM quantile regression), conformal prediction, difference-in-differences, hypothesis testing, logistic regression (GLM), clustering, scenario simulation, Monte Carlo simulation, reinforcement learning (PPO, DQN).
- **Validation:** Baselines, out-of-time evaluation, shadow runs, post-release monitoring, leakage checks, block bootstrap, retrieval evaluation (NDCG, MRR).
- **Data & Engineering:** Python (pandas, NumPy, scikit-learn), SQL (Presto, Spark SQL), ETL pipelines, Git, Docker, FastAPI, PostgreSQL, AWS (SageMaker, S3), Power BI.
- **GenAI:** Agentic workflows, AI coding agents, RAG, LangChain/LangGraph, FAISS, Anthropic and OpenAI APIs, Gymnasium and Stable-Baselines3 for RL.

## PROFESSIONAL EXPERIENCE

### Data Scientist | Uber | Kraków, Poland | Mar 2026 – Oct 2026 | Full-time

- Estimated with difference-in-differences (comparable control cities, parallel pre-trends checked) that moving drivers from fees to subscriptions raised churn ~15% relative (~2 points); tested hypotheses on its causes.
- Built a weekly driver-reactivation forecasting system for India (LightGBM quantile regression, city × product × week, 20+ cities and 3 products per city) on three years of data in Python and SQL (Presto, Spark SQL). Cut forecast error from 14% to 9% WAPE versus the incumbent manual process; used weekly by Operations and Finance.
- Quantified uncertainty with conformal intervals (96% empirical coverage at a 95% target); ran a six-week shadow period before production and monitored live performance for several weeks after release.
- Found a leak in the champion model's validation split, rebuilt the baseline and re-reported corrected metrics.
- Co-built with a colleague an agentic pull-request review tool connected to GitHub, Jira and internal tooling: it reads the README and linked ticket, analyses the change, runs tests and reports via Slack or email. As other contributors extended it, it grew to 60+ users, including members of Uber's internal AI group.

### Junior Data Scientist | Buynomics | Cologne, Germany | Jul 2025 – Jan 2026 | Full-time

- Built simulation models for 500+ Bel Group products estimating how sales respond to own-price changes and competitor moves; the client adopted them to test scenarios before commercial decisions.
- Modelled purchase decisions with logistic regression and segmented products and customers with clustering; integrated country-level data through Python ETL pipelines and defined the evaluation metrics.

### Data Analyst, BI and Visualization | Porsche AG | Stuttgart, Germany | Jul 2024 – Jan 2025 | Internship

- Automated test-bench data collection in Python, cutting manual processing time by about 70%, and built a Power BI dashboard on status, bottlenecks and cycle times used weekly by a 10-person team.

### ERP Consultant | Capgemini | Valencia, Spain | Oct 2023 – Apr 2024 | Internship

- Turned a client's needs into specifications and workflows; two teams adopted the documentation as standard.

## PROJECTS

### When to Trust an Observational Price Elasticity | Personal Project, in progress | 2026

- Pre-registered benchmark of price-elasticity estimators (log-log OLS, fixed effects, promotion controls, an instrument, double ML) on a market simulator with known elasticities, scored on profit regret; re-implements and stress-tests an ACT / WAIT / EXPERIMENT trust gate, with one real application on Dominick's data. No results yet.

### Book Recommender with Hybrid Retrieval and LLMs | Bachelor's Thesis (Computer Science) | 2025

- Book search over 177 titles (LangChain/LangGraph, FAISS, FastAPI): github.com/luisgius/AI-Book-recommender/tree/15-12-llm
- Offline evaluation on 44 Spanish/English queries: hybrid BM25 + vector retrieval (RRF) reached NDCG@10 0.542 versus 0.460 for BM25; vector-only had the best MRR.

### Stock-Market RL Lab | Personal Project | 2026

- Gymnasium trading environment with transaction costs and PPO/DQN agents (Stable-Baselines3), tested against simple baselines with a pre-declared protocol on five ETFs and a block bootstrap: github.com/luisgius/stock-market-rl-lab
- Result, reported as found: PPO trailed buy-and-hold by ~2.4 pp/year after costs.

### Pragmatic Bayes and Bounded Frequentism | Personal Project, in progress | 2026

- Compares Bayesian inference (Monte Carlo Bayes risk, compute cost) with frequentist confidence sets (Feldman–Cousins, profile likelihood) on a Poisson counting experiment with a physical boundary and nuisance parameters, checking coverage on a grid: github.com/luisgius/bayes-frequentist

### Job-Posting Scoring Pipeline | Personal Project | 2026

- Daily pipeline built with AI coding agents: collects postings from ATS boards and aggregators, filters them, scores fit with an LLM (local model first, hosted fallbacks) and drafts tailored applications, with an offline evaluation set and an extensive test suite: github.com/luisgius/job_search

### PolizIA, Traffic-Police Reference Assistant | Personal Project | 2026

- Search tool and assistant for traffic police built with AI coding agents: a local search engine over fines and offences, and an assistant that answers from versioned extracts of the Spanish official gazette (BOE) with cited evidence (FastAPI, PostgreSQL).

## EDUCATION

### Dual-Degree Programme | Universitat Politècnica de València, Spain | 2018 – 2025

- B.Sc. Business Administration (2018 – 2024): degree conferred.
- B.Sc. Computer Science (2018 – 2025): all coursework and thesis completed; official
  thesis approval and degree conferral expected Dec 2026.
- Erasmus exchange: Babeș-Bolyai University, Cluj-Napoca, Romania (2022/23).

## CERTIFICATIONS

- Machine Learning Specialization, Stanford Online / DeepLearning.AI (Coursera)
- Bayesian Statistics: From Concept to Data Analysis, UC Santa Cruz / Coursera (2026)

## LANGUAGES

- Spanish: Native
- English: Full Professional Proficiency (C1)
- German: Beginner (A2)
