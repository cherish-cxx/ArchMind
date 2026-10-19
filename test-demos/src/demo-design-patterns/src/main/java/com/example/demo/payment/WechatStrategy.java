package com.example.demo.payment;

public class WechatStrategy implements PaymentStrategy {

    @Override
    public String name() {
        return "wechat";
    }

    @Override
    public boolean pay(double amount) {
        System.out.println("wechat paid " + amount);
        return true;
    }
}
