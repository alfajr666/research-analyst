module.exports = {
  apps: [
    {
      name: "orchestrator",
      interpreter: "./venv/bin/python",
      script: "orchestrator.py",
      cwd: __dirname,
      error_file: "./logs/orchestrator-error.log",
      out_file: "./logs/orchestrator-out.log",
      merge_logs: true,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
    },
    {
      // Delivery must continue while a research pipeline cycle is rate-limited.
      name: "signal-publisher",
      interpreter: "./venv/bin/python",
      script: "signal_publisher.py",
      cwd: __dirname,
      error_file: "./logs/signal-publisher-error.log",
      out_file: "./logs/signal-publisher-out.log",
      merge_logs: true,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
    },
    // Binance OI rotation is owned by the orchestrator (single market-DB writer).
    // The feed file is still published for bot consumption; see orchestrator.py.
    // {
    //   name: "telegram-bot",
    //   interpreter: "./venv/bin/python",
    //   script: "telegram_bot.py",
    //   cwd: __dirname,
    //   error_file: "./logs/telegram_bot-error.log",
    //   out_file: "./logs/telegram_bot-out.log",
    //   merge_logs: true,
    //   autorestart: true,
    //   max_restarts: 10,
    //   restart_delay: 5000,
    // },
  ],
};
