package com.example.demo.service.impl;

import com.example.demo.exception.OrderNotFoundException;
import com.example.demo.model.Order;
import com.example.demo.model.OrderRequest;
import com.example.demo.repository.OrderRepository;
import com.example.demo.service.OrderService;
import com.example.demo.service.PricingService;

import java.util.List;

public class OrderServiceImpl implements OrderService {

    private final OrderRepository orderRepository;
    private final PricingService pricingService;

    public OrderServiceImpl(OrderRepository orderRepository, PricingService pricingService) {
        this.orderRepository = orderRepository;
        this.pricingService = pricingService;
    }

    @Override
    public Order create(OrderRequest request) {
        double price = pricingService.calculate(request.getAmount(), request.getCouponCode());
        Order draft = new Order(0, request.getCustomerName(), price, "CREATED");
        return orderRepository.save(draft);
    }

    @Override
    public Order get(long id) {
        return orderRepository.findById(id)
                .orElseThrow(() -> new OrderNotFoundException(id));
    }

    @Override
    public List<Order> list() {
        return orderRepository.findAll();
    }
}
