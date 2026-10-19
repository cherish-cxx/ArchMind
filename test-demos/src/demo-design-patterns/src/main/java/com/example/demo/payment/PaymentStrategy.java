package com.example.demo.payment;

public interface PaymentStrategy {

    String name();

    boolean pay(double amount);
}
