package com.example.demo.controller;

import com.example.demo.model.Order;
import com.example.demo.model.OrderRequest;
import com.example.demo.service.OrderService;

import java.util.List;

public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    public Order createOrder(OrderRequest request) {
        return orderService.create(request);
    }

    public Order getOrder(long id) {
        return orderService.get(id);
    }

    public List<Order> listOrders() {
        return orderService.list();
    }
}
