"""Regenerates sample_data/incidents.json and sample_data/logs/app.log (10 incidents + noise).

Usage: python sample_data/generate.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

TEMPLATES = {
    "npe": ('java.lang.NullPointerException: Cannot invoke "Customer.getAddress()" because "c" is null',
            ["com.shop.order.OrderService.shippingLabel(OrderService.java:15)", "com.shop.web.CheckoutController.label(CheckoutController.java:58)"],
            "OrderService.java", "NullPointerException", "SocketTimeoutException"),
    "timeout": ("java.net.SocketTimeoutException: Read timed out",
                ["com.shop.payment.PaymentClient.charge(PaymentClient.java:11)", "com.shop.web.PayController.pay(PayController.java:41)"],
                "PaymentClient.java", "SocketTimeoutException", None),
    "pool": ("HikariPool-1 - Connection is not available, request timed out after 30000ms (total=5, active=5, idle=0, waiting=37)",
             [], "DataSourceConfig.java", "Hikari", None),
    "json": ("com.fasterxml.jackson.databind.exc.UnrecognizedPropertyException: Unrecognized field \"taxCode\"",
             ["com.shop.billing.InvoiceParser.parse(InvoiceParser.java:9)", "com.shop.billing.ImportJob.run(ImportJob.java:27)"],
             "InvoiceParser.java", "UnrecognizedPropertyException", None),
    "oom": ("java.lang.OutOfMemoryError: Java heap space",
            ["com.shop.report.ReportService.exportAll(ReportService.java:9)", "com.shop.report.NightlyJob.run(NightlyJob.java:19)"],
            "ReportService.java", "OutOfMemoryError", None),
}

DESCRIPTIONS = {
    "npe": ["Checkout returns HTTP 500 when printing a shipping label. NullPointerException in the order service.",
            "Support reports orders stuck at the label step; alerts show NullPointerException for some customers."],
    "timeout": ["Payments hang then fail intermittently with SocketTimeoutException during charge.",
                "Card charges are slow and some fail: SocketTimeoutException from the payment client."],
    "pool": ["API latency spike and 503s: HikariPool reports connections are not available.",
             "All endpoints timing out, HikariPool exhausted under load after the last deploy."],
    "json": ["Invoice import failing: UnrecognizedPropertyException when parsing partner payloads.",
             "Billing sync broke after partner API update, UnrecognizedPropertyException on invoice JSON."],
    "oom": ["Nightly report export crashes with OutOfMemoryError (Java heap space).",
            "Report service pod restarting repeatedly, OutOfMemoryError during export."],
}

ORDER = ["npe", "timeout", "pool", "json", "oom"]
NOISE_EXC = ["NullPointerException", "SocketTimeoutException", "UnrecognizedPropertyException", "OutOfMemoryError"]


def main() -> None:
    incidents, lines = [], []
    n = 0
    for variant in range(2):
        for cat in ORDER:
            n += 1
            inc_id = f"INC-{100 + n}"
            msg, frames, expected, *_ = TEMPLATES[cat]
            incidents.append({"id": inc_id, "description": DESCRIPTIONS[cat][variant], "expected_file": expected})
            ts = f"2026-11-{10 + variant:02d} 0{n % 10}:1{n % 6}:0{n % 9}"
            lines.append(f"{ts} ERROR [{inc_id}] {msg}")
            lines += [f"\tat {f}" for f in frames]
            # interleave some noise after each incident
            for k in range(3):
                lines.append(f"{ts} INFO  [req-{n}{k}] request completed in {40 + 13 * k}ms")
            exc = NOISE_EXC[n % len(NOISE_EXC)]
            lines.append(f"{ts} WARN  [req-{n}9] recovered from {exc} on retry {n % 3 + 1}")
    (HERE / "logs").mkdir(exist_ok=True)
    (HERE / "logs" / "app.log").write_text("\n".join(lines) + "\n")
    (HERE / "incidents.json").write_text(json.dumps(incidents, indent=2) + "\n")
    print(f"wrote {len(incidents)} incidents and {len(lines)} log lines")


if __name__ == "__main__":
    main()
