package com.example.demo.repository.impl;

import com.example.demo.model.Order;
import com.example.demo.repository.OrderRepository;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

public class InMemoryOrderRepository implements OrderRepository {

    private final Map<Long, Order> store = new ConcurrentHashMap<>();
    private final AtomicLong sequence = new AtomicLong(1000);

    @Override
    public Order save(Order order) {
        long id = order.getId() > 0 ? order.getId() : sequence.incrementAndGet();
        Order stored = new Order(id, order.getCustomerName(), order.getAmount(), order.getStatus());
        store.put(id, stored);
        return stored;
    }

    @Override
    public Optional<Order> findById(long id) {
        return Optional.ofNullable(store.get(id));
    }

    @Override
    public List<Order> findAll() {
        return new ArrayList<>(store.values());
    }
}
