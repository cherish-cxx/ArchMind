package com.example.demo.order;

public class Order {

    private final long id;
    private final String customerName;
    private final double amount;
    private final String paymentType;
    private String status;

    public Order(long id, String customerName, double amount, String paymentType, String status) {
        this.id = id;
        this.customerName = customerName;
        this.amount = amount;
        this.paymentType = paymentType;
        this.status = status;
    }

    public long getId() {
        return id;
    }

    public String getCustomerName() {
        return customerName;
    }

    public double getAmount() {
        return amount;
    }

    public String getPaymentType() {
        return paymentType;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }
}
