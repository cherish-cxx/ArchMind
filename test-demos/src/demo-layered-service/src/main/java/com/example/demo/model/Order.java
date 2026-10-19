package com.example.demo.model;

public class Order {

    private final long id;
    private final String customerName;
    private final double amount;
    private String status;

    public Order(long id, String customerName, double amount, String status) {
        this.id = id;
        this.customerName = customerName;
        this.amount = amount;
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

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }
}
