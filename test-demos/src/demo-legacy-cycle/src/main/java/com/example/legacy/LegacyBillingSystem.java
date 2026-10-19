package com.example.legacy;

import com.example.legacy.model.Invoice;
import com.example.legacy.util.MoneyUtils;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

public class LegacyBillingSystem {

    public static final String VERSION = "legacy-1.0";

    private final BillingCalculator calculator;
    private final DiscountPolicy discountPolicy;

    public LegacyBillingSystem(BillingCalculator calculator, DiscountPolicy discountPolicy) {
        this.calculator = calculator;
        this.discountPolicy = discountPolicy;
    }

    public double calculate(Invoice invoice) {
        return calculate(invoice, true);
    }

    public double calculate(Invoice invoice, boolean applyDiscount) {
        double base = calculator.baseAmount(invoice);
        double taxed = calculator.withTax(base);
        if (applyDiscount) {
            taxed = discountPolicy.apply(taxed, invoice.getCustomer());
        }
        return MoneyUtils.round(taxed);
    }

    public String report(List<Invoice> invoices) {
        List<ReportRow> rows = new ArrayList<>();
        for (Invoice invoice : invoices) {
            rows.add(new ReportRow(invoice.getCustomer().getName(), calculate(invoice)));
        }
        rows.sort(new Comparator<ReportRow>() {
            @Override
            public int compare(ReportRow left, ReportRow right) {
                return Double.compare(right.amount, left.amount);
            }
        });

        StringBuilder builder = new StringBuilder();
        for (ReportRow row : rows) {
            builder.append(row.customer).append('=').append(row.amount).append('\n');
        }
        return builder.toString();
    }

    public static String version() {
        return VERSION;
    }

    public class ReportRow {

        private final String customer;
        private final double amount;

        public ReportRow(String customer, double amount) {
            this.customer = customer;
            this.amount = amount;
        }
    }
}
