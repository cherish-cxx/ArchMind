package com.example.demo.payment;

import java.util.HashMap;
import java.util.Map;

public class PaymentStrategyFactory {

    private static final Map<String, PaymentStrategy> STRATEGIES = new HashMap<>();

    static {
        register(new AlipayStrategy());
        register(new WechatStrategy());
        register(new CardStrategy());
    }

    private PaymentStrategyFactory() {
    }

    public static void register(PaymentStrategy strategy) {
        STRATEGIES.put(strategy.name(), strategy);
    }

    public static PaymentStrategy get(String name) {
        PaymentStrategy strategy = STRATEGIES.get(name);
        if (strategy == null) {
            throw new IllegalArgumentException("unsupported payment: " + name);
        }
        return strategy;
    }
}
