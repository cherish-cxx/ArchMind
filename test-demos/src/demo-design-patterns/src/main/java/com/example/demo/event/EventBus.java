package com.example.demo.event;

import com.example.demo.order.Order;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

public class EventBus {

    private final List<OrderEventListener> listeners = new CopyOnWriteArrayList<>();

    public void register(OrderEventListener listener) {
        listeners.add(listener);
    }

    public void publish(Order order) {
        for (OrderEventListener listener : listeners) {
            listener.onOrderCreated(order);
        }
    }

    public int listenerCount() {
        return listeners.size();
    }
}
