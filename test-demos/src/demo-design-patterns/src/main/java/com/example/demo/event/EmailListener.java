package com.example.demo.event;

import com.example.demo.order.Order;

public class EmailListener implements OrderEventListener {

    @Override
    public void onOrderCreated(Order order) {
        System.out.println("email sent to " + order.getCustomerName() + " for order " + order.getId());
    }
}
