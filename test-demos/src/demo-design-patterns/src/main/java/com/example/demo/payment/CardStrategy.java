package com.example.demo.payment;

public class CardStrategy implements PaymentStrategy {

    @Override
    public String name() {
        return "card";
    }

    @Override
    public boolean pay(double amount) {
        if (amount > 5000) {
            System.out.println("card limit exceeded");
            return false;
        }
        System.out.println("card paid " + amount);
        return true;
    }
}
