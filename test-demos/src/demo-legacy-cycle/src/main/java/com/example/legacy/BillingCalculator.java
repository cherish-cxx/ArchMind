package com.example.legacy;

import com.example.legacy.model.Invoice;
import com.example.legacy.util.MoneyUtils;

public class BillingCalculator {

    public double baseAmount(Invoice invoice) {
        double total = 0;
        for (Invoice.LineItem item : invoice.getItems()) {
            total += item.getSubtotal();
        }
        return MoneyUtils.round(total);
    }

    public double withTax(double amount) {
        return MoneyUtils.round(amount * 1.08);
    }
}
