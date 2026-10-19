package com.example.demo.service;

public class PricingService {

    public double calculate(double amount, String couponCode) {
        double discount = resolveDiscount(couponCode);
        return round(amount * (1 - discount));
    }

    private double resolveDiscount(String couponCode) {
        if (couponCode == null) {
            return 0;
        }
        switch (couponCode) {
            case "VIP":
                return 0.2;
            case "NEW":
                return 0.1;
            default:
                return 0;
        }
    }

    private double round(double value) {
        return Math.round(value * 100.0) / 100.0;
    }
}
