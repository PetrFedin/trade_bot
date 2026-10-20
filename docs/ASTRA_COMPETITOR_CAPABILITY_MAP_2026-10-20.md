# ASTRA 2026: Competitive Capability Audit and Release Roadmap
Дата: 2026-10-20. Контур: исследовательский. Стратегия A и полномочия торговли неизменны.

## Проверка источников
Этот аудит сравнивает *документированные* возможности аналогов, а не независимо
проверенную производительность их торговых ботов. Основа по ASTRA — просмотр
PetrFedin/trade_bot app/, tools/, tests/, docs/ и точные PR #324, #326–328.
Статусы означают только наблюдаемое: EXISTS_CODE / DRAFT / PARTIAL / PLANNED /
BLOCKED / REVIEW. Не делать вывод «функции нет» лишь по одному исходному файлу.

## Сопоставленные зарубежные решения
| Платформа | Проверенные функции | Урок для ASTRA | Источник |
| --- | --- | --- | --- |
| Freqtrade | Backtest, hyperopt, lookahead-analysis, MaxDrawdown / StoplossGuard / cooldown | P0 поиск утечек, P1 паузы входов только в shadow | https://www.freqtrade.io/en/stable/lookahead-analysis/ ; https://www.freqtrade.io/en/stable/plugins/ |
| QuantConnect LEAN | Research, datafeed/transactions modules, multi-asset backtest/live | Версии экспериментов; единая нормализация, независимые брокерские права | https://www.quantconnect.com/docs/v2/lean-engine/getting-started |
| Hummingbot | Controllers/Executors, market making, Dashboard, strategies V2 | Раздельные эксперименты и портфельный риск-контроль | https://hummingbot.org/strategies/v2-strategies/controllers/ |
| NautilusTrader | Event-driven trading, exchange adapters, granular order-book simulation | P0 сравнить modelled fills с event-by-event fills, не подменять стакан свечами | https://nautilustrader.io/docs/latest/concepts/backtesting/data-and-venues/ |
| TradingView | Pine Strategy Tester, Bar Magnifier lower TF, broker emulator, alerts | P0 first touch и gap handling, P1 визуализация, объяснение допущений | https://www.tradingview.com/support/solutions/43000669285-what-is-bar-magnifier-backtesting-mode/ ; https://www.tradingview.com/support/solutions/43000786181-broker-emulator/ |
| MetaTrader 5 | Multicurrency Strategy Tester, distributed optimization agents | P2 распараллелить исследование с лимитом экспериментов и запретом OOS cherry-picking | https://www.metatrader5.com/en/terminal/help/algotrading/testing |
| 3Commas | SmartTrade, bots DCA/Grid/Signal, trailing | P1 прозрачное ручное управление конфигурациями; DCA не включать по умолчанию | https://3commas.io/features |
| Cryptohopper | No-code strategy designer, backtesting, paper trading | P1 типизированный DSL и maker-check; без авто-включения live | https://www.cryptohopper.com/features/strategy-designer |

## Сопоставленные российские решения
| Платформа | Проверенные функции | Урок для ASTRA | Источник |
| --- | --- | --- | --- |
| TSLab | Визуальный редактор торговых сценариев, backtesting, optimize/agents | P1 визуальный pipeline стратегий, общий портфельный риск-контроль | https://www.tslab.ru/ |
| OsEngine | OsTrader/OsTester/OsOptimizer/OsData, несколько подключений | P1 библиотека стратегий и отдельные дата-адаптеры; код нельзя копировать без прав | https://github.com/AlexWan/OsEngine |
| T-Invest API | gRPC/REST/WS, sandbox, streaming, market data, account/order APIs | P2 broker conformance, sandbox contract, separate read-only tokens | https://developer.tbank.ru/invest/intro/intro ; https://developer.tbank.ru/invest/intro/developer/sandbox |
| ALOR OpenAPI | Документированные REST/WS брокерские API | P2 адаптер с контрактной схемой, legal/data review | https://alor.dev/ |
| Finam Trade API | Документированные data и trading сервисы | P2 отдельный адаптер и статус прав API | https://tradeapi.finam.ru/docs/guides/grpc/marketdata_service/ |

## Полная карта развития
| Код | Возможность | Наблюдаемый статус | Приоритет | Зависимость / критерий приёмки |
| --- | --- | --- | --- | --- |
| DATA-01 | Исторические price/funding snapshots, hash manifests | DRAFT #324 | P0 | Только реальный источник, проверка полноты |
| DATA-02 | Intrabar first-touch 1h/5m | DRAFT #324 | P0 | Не притворяться venue fill |
| DATA-03 | Settlement Mark Price | BLOCKED HTTP403 #326 | P0 | Подписанный/независимо квалифицированный архив |
| DATA-04 | Проверка bid/ask спреда | PLANNED | P0 | Нужен исторический top-of-book |
| DATA-05 | L2 depth и queue | PLANNED | P1 | Сырые order-book события |
| DATA-06 | История спецификаций instrument tick/lot | PARTIAL | P0 | Point-in-time valid_from/valid_to |
| DATA-07 | Асинхронные gap/duplicate/out-of-order | EXISTS_CODE/REVIEW | P0 | Mutation tests |
| DATA-08 | Мультибиржевой коннектор read-only | PARTIAL | P1 | Broker ID + provenance |
| DATA-09 | Режимы корпоративных действий FX/дивиденды | PLANNED | P2 | Noncrypto market schema |
| DATA-10 | Раздельная лицензия и аудит данных | PLANNED | P0 | Правовые условия по провайдеру |
| REPLAY-01 | 36-сценарный исторический cash journal | DRAFT #324/#328 | P0 | Exact-head SHA, secondary verifier |
| REPLAY-02 | Research Control Tower статический | NEW DRAFT | P0 | Статус authority всегда HOLD |
| REPLAY-03 | Отдельные 90-дневные окна / полные хвосты | DRAFT #328 | P0 | Не приравнивать к нетронутому holdout |
| REPLAY-04 | Monetary reconciliation | DRAFT #326/#328 | P0 | Trade/funding/equity populations |
| REPLAY-05 | Unambiguous stop/target chronology | PARTIAL | P0 | До внутрибара остается неопределенность |
| REPLAY-06 | Bias test (lookahead) | REVIEW | P0 | Point-in-time feature whitelist |
| REPLAY-07 | Purged/embargo time-series CV | PLANNED | P1 | Freeze+audit selected hypotheses |
| REPLAY-08 | Мультисимвольный same-capital benchmark | PARTIAL | P0 | Identical calendar + exposure |
| REPLAY-09 | Transaction cost grid | DRAFT #324 | P0 | Fee/slip/funding/mark/impact |
| REPLAY-10 | Нетронутый следующий период | BLOCKED | P0 | Time after preregistered freeze |
| REPLAY-11 | Monte Carlo execution perturbations | PLANNED | P1 | Variance not evidence of source truth |
| REPLAY-12 | Forced outage/latency simulation | REVIEW | P1 | Separate event-order scenarios |
| REPLAY-13 | Реалистичный derivatives margin/liquidation | BLOCKED | P0 | Venue account and margin rules |
| REPLAY-14 | Order entry timing guard next bar/tick | REVIEW | P0 | No close-bar lookahead |
| REPLAY-15 | Offline provenance and content pinning | DRAFT #327/#328 | P0 | Atomic immutable receipts |
| RISK-01 | Максимальная портфельная экспозиция | EXISTS_CODE | P0 | End-of-day + intra-event assertions |
| RISK-02 | Запрет кредитного плеча в исследовании | EXISTS_CODE | P0 | No user escape via UI |
| RISK-03 | Reentry confirmation | EXISTS_CODE | P0 | Maintain state history |
| RISK-04 | ARM/HALT | EXISTS_CODE | P0 | No AI or browser actuator |
| RISK-05 | Exclusive OMS order mutations | EXISTS_CODE | P0 | No double submit/replacement |
| RISK-06 | Portfolio reconciliation | EXISTS_CODE | P0 | Reject stale account snapshot |
| RISK-07 | Per-symbol exposure and loss budget | PLANNED | P1 | Research only, past-only rule |
| RISK-08 | Circuit breaker by consecutive stops | PLANNED | P1 | Predeclared, tested against no breaker |
| RISK-09 | MaximumDrawdown / equity halt | PARTIAL/REVIEW | P0 | Portfolio risk independent of strategy |
| RISK-10 | Order gap/reject/partial fill model | REVIEW | P0 | Broker conformance suite |
| RISK-11 | Concentration/correlation cap across bots | PLANNED | P1 | Shared risk book mandatory |
| RISK-12 | Funding shock / liquidation buffer | PLANNED | P0 | Historical funding + mark |
| STRAT-01 | Shipped Strategy A as frozen control | EXISTS_CODE | P0 | Immutable selection/exit |
| STRAT-02 | Candidate B impulse prior-bar veto | DRAFT #321/#322 | P1 | No uninspected OOS claim |
| STRAT-03 | ATR-per-coin experiments | DRAFT #319 | P1 | Retain all negative arms |
| STRAT-04 | Regime switching | PLANNED | P1 | No optimize-on-test |
| STRAT-05 | Typed graphical research builder | PLANNED | P1 | Validate DSL before research |
| STRAT-06 | Strategy registry / config signatures | PARTIAL/PLANNED | P1 | Git+data+config SHA |
| STRAT-07 | Multi-strategy allocation | PLANNED | P1 | Single risk gate + capital ledger |
| STRAT-08 | Market-making | PLANNED | P2 | Order book + fees + inventory |
| STRAT-09 | DCA/Grid | PLANNED | P2 | Tail-loss analysis, no Martingale default |
| STRAT-10 | Statistical arbitrage | PLANNED | P2 | Hedging, basis/funding/correlation |
| STRAT-11 | Multi-exchange spread research | PLANNED | P2 | Fee, latency, withdrawal constraints |
| STRAT-12 | RL/GenAI strategy research | PLANNED | P2 | Offline-only; hard promotion firewall |
| UI-01 | Operational status console | EXISTS_CODE | P0 | Separate source-of-truth |
| UI-02 | Trade-by-trade investigation | NEW DRAFT | P0 | Mark hypothetical fills |
| UI-03 | Quarter-by-quarter 90-day PnL | NEW DRAFT | P0 | No annualizing from sample |
| UI-04 | Multi-universe same-period scenario panel | NEW DRAFT | P0 | 36-model provenance checks |
| UI-05 | Fees / funding / slippage breakdown | NEW DRAFT | P0 | Trace independent auditor |
| UI-06 | RU/EN i18n and mobile | PARTIAL/PLANNED | P1 | WCAG and localization QA |
| UI-07 | Explainable why-entry/why-block | PARTIAL/PLANNED | P1 | State graph + causal inputs |
| UI-08 | AI analyst without trade privileges | PLANNED | P1 | Read-only outputs tagged research |
| UI-09 | Local export HTML/JSON/CSV | PARTIAL | P1 | No unreviewed data disclosure |
| UI-10 | Screenshot/video true UI evidence | PLANNED | P2 | Only from real UI, no fake visuals |
| CONNECT-01 | T-Invest read-only conformance | PLANNED | P2 | Sandbox, legal and tokens |
| CONNECT-02 | ALOR read-only conformance | PLANNED | P2 | Protocol errors/rate limits |
| CONNECT-03 | Finam read-only conformance | PLANNED | P2 | Exchange-spec readiness |
| CONNECT-04 | TradingView read-only webhook adapter | PLANNED | P2 | Untrusted alerts cannot create orders |
| CONNECT-05 | Telemetry / audit event export | PARTIAL | P1 | Least privilege, no keys |
| GOVERN-01 | Immutable evidence SHA + report | DRAFT #328 | P0 | Atomic publication |
| GOVERN-02 | Provenance verified by independent party | BLOCKED | P0 | Signature/trusted channel |
| GOVERN-03 | Research cannot promote itself | EXISTS_CODE | P0 | Explicit promotion manifest only |
| GOVERN-04 | Russian market data licenses | PLANNED | P0 | Source terms, user consent, lawful data |
| GOVERN-05 | Strategy copy marketplace | NOT_ADOPTED | P2 | Compliance and commercial licensing |
| GOVERN-06 | Auto LIVE on positive backtest | REJECTED | NEVER | Excluded by design |
| GOVERN-07 | Hidden loss removal from reports | REJECTED | NEVER | Negative evidence retained |
| GOVERN-08 | Provider API restriction circumvention | REJECTED | NEVER | Separate permitted data source |

## Phased acceptance
P0: immutable Control Tower; adversarial digest/HTML tests; original 36-model
replay and *separate* audited money receipt before rendering, full GitHub CI.
P1: typed research-only strategy DSL, causal dataset guards, explainable
decision trace, bankroll limits and model calibration on already inspected
data as discovery only.
P2: alternative strategies, new connector classes and possible commercial
extension after legal review and independently measured economic viability.
The original Strategy A and live/paper routing must not change.

User interface: dark neutral professional design, compact KPI, same-date
six-vs-21, six scenario groups each with six model variants, full 90-day
capital audit and symbol/trade drilldown, prominent live HOLD, offline CSP.
No interactive button may claim to submit an order or grant permission.


## Detached source pin for this exact research batch
The offline Control Tower also requires an explicitly supplied detached expected
journal SHA-256. The stacked exact-head PR328 replay's independently verified
journal digest is:
e2c18b0eb0805a7f9b737f6290d601227ba7c6fb20688a6e7f9a299fb431bfc9
CI must pass this previously reviewed digest in --expected-journal-sha256.
A newly self-hashed replacement is refused even if its internal totals balance.
Changing this frozen source requires a new reviewed source-admission decision.
This is NOT cryptographic exchange authorship or a future holdout.
