package com.example.demo.event;

import com.example.demo.order.Order;

public interface OrderEventListener {

    void onOrderCreated(Order order);
}
