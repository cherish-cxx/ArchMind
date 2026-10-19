package com.example.demo.model;

public class OrderRequest {

    private final String customerName;
    private final double amount;
    private final String couponCode;

    public OrderRequest(String customerName, double amount, String couponCode) {
        this.customerName = customerName;
        this.amount = amount;
        this.couponCode = couponCode;
    }

    public String getCustomerName() {
        return customerName;
    }

    public double getAmount() {
        return amount;
    }

    public String getCouponCode() {
        return couponCode;
    }
}
