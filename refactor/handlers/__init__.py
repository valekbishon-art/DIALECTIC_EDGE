"""
refactor/handlers - Decomposed Telegram bot handlers

Provides structured handler modules for different bot commands and features:

    debate_handler   - Debate navigation and round display
    market_handler   - Market analysis and /market command
    profile_handler  - User profile management
    admin_handler    - Administrative commands
    utils            - Shared utility functions

Quick Start:

    # Import handlers
    from refactor.handlers import (
        get_debate_handler,
        get_market_handler,
        get_profile_handler,
        get_admin_handler,
    )

    # Import utilities
    from refactor.handlers.utils import (
        split_message,
        clean_markdown,
        parse_report_parts,
        debates_keyboard,
        main_report_keyboard,
    )

    # Import public functions
    from refactor.handlers import (
        store_and_link_debate,
        show_debate_round,
        handle_market_command,
        show_profile,
        handle_stats_command,
    )

Handler Architecture:

    Each handler manages a specific domain:

    ┌─────────────────────────────────────────────┐
    │          Telegram Bot (main.py)             │
    └─────────────────────────────────────────────┘
                        ↓
    ┌────────────────────────────────────────────────────────┐
    │                    Handlers                            │
    ├──────────────┬──────────────┬────────────┬─────────────┤
    │   Market     │   Debate     │  Profile   │    Admin    │
    │  Handler     │  Handler     │  Handler   │  Handler    │
    └──────────────┴──────────────┴────────────┴─────────────┘
                        ↓
    ┌────────────────────────────────────────────────────────┐
    │            Handler Utils (shared functions)            │
    │  - split_message, clean_markdown, parse_report_parts,  │
    │    debates_keyboard, main_report_keyboard, etc.        │
    └────────────────────────────────────────────────────────┘
                        ↓
    ┌────────────────────────────────────────────────────────┐
    │            Refactor Models & Utils                     │
    │  - FinalReport, UserProfile, AnalysisContext, etc.     │
    └────────────────────────────────────────────────────────┘

Module Index:

    debate_handler.py (320 lines)
        - DebateHandler class
        - store_and_link_debate()
        - show_debate_round()
        - handle_debate_navigation_callback()
        - hydrate_debate_from_report()

    market_handler.py (280 lines)
        - MarketHandler class
        - handle_market_command()
        - parse_market_command()
        - get_supported_markets()
        - get_market_examples()

    profile_handler.py (300 lines)
        - ProfileHandler class
        - load_or_create_profile()
        - show_profile()
        - show_risk_selection()
        - show_horizon_selection()
        - show_markets_selection()

    admin_handler.py (280 lines)
        - AdminHandler class
        - is_admin()
        - register_admin()
        - handle_stats_command()
        - handle_health_command()
        - handle_logs_command()
        - handle_sysinfo_command()

    utils.py (220 lines)
        - split_message()
        - clean_markdown()
        - parse_report_parts()
        - debates_keyboard()
        - main_report_keyboard()
        - extract_signal_pct_and_stars()
        - signal_to_stars()
        - build_short_report()

Migration from main.py:

    Step 1: Extract handlers from main.py
        - /market command → use refactor.handlers.market_handler
        - Debate navigation → use refactor.handlers.debate_handler
        - /profile command → use refactor.handlers.profile_handler
        - /stats etc → use refactor.handlers.admin_handler

    Step 2: Register handlers in main.py
        from refactor.handlers import (
            handle_market_command,
            store_and_link_debate,
            show_profile,
        )

        # In main() setup:
        @dp.message_handler(commands=['market'])
        async def cmd_market(message: Message):
            await handle_market_command(message, message.text)

    Step 3: Update callbacks
        from refactor.handlers import handle_debate_navigation_callback

        @dp.callback_query_handler(...)
        async def handle_debate_nav(callback: CallbackQuery):
            round_idx = extract_from_callback(callback.data)
            await handle_debate_navigation_callback(
                callback,
                user_id=callback.from_user.id,
                round_idx=round_idx,
            )

Integration Checklist:

    ✅ All handlers created with singleton patterns
    ✅ Public export functions for easy integration
    ✅ Shared utils module for text processing
    ✅ Type-safe with dataclass models
    ✅ Comprehensive docstrings and examples
    ⏳ Import migrations in main.py (pending)
    ⏳ Full integration testing (pending)

Phase 3 Status:

    Created Files:
    ✅ refactor/handlers/utils.py (220 LOC)
    ✅ refactor/handlers/debate_handler.py (320 LOC)
    ✅ refactor/handlers/market_handler.py (280 LOC)
    ✅ refactor/handlers/profile_handler.py (300 LOC)
    ✅ refactor/handlers/admin_handler.py (280 LOC)
    ✅ refactor/handlers/__init__.py (this file)

    Phase 3 Progress: 95% (1400 LOC created, imports pending)

    Remaining: Import migrations in main.py + integration testing

"""

# Core handler instances
from .debate_handler import (
    get_debate_handler,
    store_and_link_debate,
    show_debate_round,
    handle_debate_navigation_callback,
)

from .market_handler import (
    get_market_handler,
    handle_market_command,
    parse_market_command,
    get_supported_markets,
    get_market_examples,
)

from .profile_handler import (
    get_profile_handler,
    load_or_create_profile,
    show_profile,
    show_profile_settings,
    handle_profile_callback,
    show_risk_selection,
    show_horizon_selection,
    show_markets_selection,
)

from .portfolio_handler import (
    show_portfolio,
    handle_portfolio_callback,
    handle_portfolio_text_input,
    cmd_add_portfolio,
    cmd_remove_portfolio,
)

from .admin_handler import (
    get_admin_handler,
    is_admin,
    register_admin,
    check_admin,
    handle_stats_command,
    handle_health_command,
    handle_logs_command,
    handle_sysinfo_command,
    setup_admins,
)

from .funding_handler import (
    FUNDING_SYMBOLS,
    FundingRow,
    classify_funding,
    fetch_funding_rates,
    format_funding_report,
    handle_funding_command,
    register_funding_handlers,
)

from .btc_handler import (
    build_btc_outlook_message,
    fetch_btc_outlook_inputs,
    handle_btc_command,
    register_btc_handlers,
)

from .sniping_handler import (
    SniperPlan,
    SniperReport,
    build_sniper_plans_for_asset,
    format_sniper_report,
    handle_sniping_callback,
    handle_sniping_command,
    parse_sniping_callback_data,
    register_sniping_handlers,
    sniping_callback_data,
)

from .p2p_arbitrage_handler import (
    fetch_binance_p2p_ads,
    fetch_bybit_p2p_ads,
    fetch_p2p_ads,
    handle_p2p_command,
    register_p2p_arbitrage_handlers,
)

from .postmortem_handler import (
    handle_postmortem_command,
    register_postmortem_handlers,
)

from .retro_handler import (
    handle_retro_command,
    register_retro_handlers,
)

# Utilities
from .utils import (
    split_message,
    clean_markdown,
    parse_report_parts,
    debates_keyboard,
    main_report_keyboard,
    extract_signal_pct_and_stars,
    signal_to_stars,
    build_short_report,
    find_debate_start_index,
)

__all__ = [
    # Debate Handler
    "get_debate_handler",
    "store_and_link_debate",
    "show_debate_round",
    "handle_debate_navigation_callback",

    # Market Handler
    "get_market_handler",
    "handle_market_command",
    "parse_market_command",
    "get_supported_markets",
    "get_market_examples",

    # Profile Handler
    "get_profile_handler",
    "load_or_create_profile",
    "show_profile",
    "show_profile_settings",
    "handle_profile_callback",
    "show_risk_selection",
    "show_horizon_selection",
    "show_markets_selection",

    # Portfolio Handler
    "show_portfolio",
    "handle_portfolio_callback",
    "handle_portfolio_text_input",
    "cmd_add_portfolio",
    "cmd_remove_portfolio",

    # Admin Handler
    "get_admin_handler",
    "is_admin",
    "register_admin",
    "check_admin",
    "handle_stats_command",
    "handle_health_command",
    "handle_logs_command",
    "handle_sysinfo_command",
    "setup_admins",

    # Utilities
    "split_message",
    "clean_markdown",
    "parse_report_parts",
    "fetch_binance_p2p_ads",
    "fetch_bybit_p2p_ads",
    "fetch_p2p_ads",
    "handle_p2p_command",
    "register_p2p_arbitrage_handlers",
    "handle_postmortem_command",
    "register_postmortem_handlers",
    "handle_retro_command",
    "register_retro_handlers",
    "build_btc_outlook_message",
    "fetch_btc_outlook_inputs",
    "handle_btc_command",
    "register_btc_handlers",
    "debates_keyboard",
    "main_report_keyboard",
    "extract_signal_pct_and_stars",
    "signal_to_stars",
    "build_short_report",
    "find_debate_start_index",
]
