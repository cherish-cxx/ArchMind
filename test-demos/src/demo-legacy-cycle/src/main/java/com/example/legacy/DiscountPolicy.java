package com.example.legacy;

import com.example.legacy.model.Customer;
import com.example.legacy.util.MoneyUtils;

public class DiscountPolicy {

    public double apply(double amount, Customer customer) {
        double rate = vipRate(customer);
        return MoneyUtils.round(amount * (1 - rate));
    }

    public double vipRate(Customer customer) {
        if (customer == null) {
            return 0;
        }
        return customer.isVip() ? 0.15 : 0.02;
    }
}
