package com.example.demo.event;

import com.example.demo.order.Order;

public class AuditListener implements OrderEventListener {

    @Override
    public void onOrderCreated(Order order) {
        System.out.println("audit recorded: " + order.getId() + " " + order.getStatus());
    }
}
