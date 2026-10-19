package com.example.demo.order;

import com.example.demo.event.EventBus;
import com.example.demo.payment.PaymentStrategy;
import com.example.demo.payment.PaymentStrategyFactory;
import com.example.demo.util.IdGenerator;

public class OrderService {

    private final EventBus eventBus;

    public OrderService(EventBus eventBus) {
        this.eventBus = eventBus;
    }

    public Order placeOrder(String customerName, double amount, String paymentType) {
        Order order = new Order(IdGenerator.nextId(), customerName, amount, paymentType, "PAID");
        PaymentStrategy strategy = PaymentStrategyFactory.get(paymentType);
        boolean success = strategy.pay(amount);
        if (!success) {
            order.setStatus("FAILED");
        }
        eventBus.publish(order);
        return order;
    }
}
