package com.example.demo.payment;

public class AlipayStrategy implements PaymentStrategy {

    @Override
    public String name() {
        return "alipay";
    }

    @Override
    public boolean pay(double amount) {
        System.out.println("alipay paid " + amount);
        return true;
    }
}
