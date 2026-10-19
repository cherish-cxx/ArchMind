package com.example.demo;

import com.example.demo.event.AuditListener;
import com.example.demo.event.EmailListener;
import com.example.demo.event.EventBus;
import com.example.demo.event.InventoryListener;
import com.example.demo.order.Order;
import com.example.demo.order.OrderService;

public class DemoApplication {

    public static void main(String[] args) {
        EventBus eventBus = new EventBus();
        eventBus.register(new EmailListener());
        eventBus.register(new InventoryListener());
        eventBus.register(new AuditListener());

        OrderService orderService = new OrderService(eventBus);

        Order first = orderService.placeOrder("Alice", 120.0, "alipay");
        Order second = orderService.placeOrder("Bob", 6000.0, "card");

        System.out.println("first=" + first.getStatus() + " listeners=" + eventBus.listenerCount());
        System.out.println("second=" + second.getStatus());
    }
}
