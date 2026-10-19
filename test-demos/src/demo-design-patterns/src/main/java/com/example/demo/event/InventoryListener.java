package com.example.demo.event;

import com.example.demo.order.Order;

public class InventoryListener implements OrderEventListener {

    @Override
    public void onOrderCreated(Order order) {
        System.out.println("inventory reserved for order " + order.getId());
    }
}
