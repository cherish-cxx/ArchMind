package com.example.demo;

import com.example.demo.controller.OrderController;
import com.example.demo.model.Order;
import com.example.demo.model.OrderRequest;
import com.example.demo.repository.OrderRepository;
import com.example.demo.repository.impl.InMemoryOrderRepository;
import com.example.demo.service.OrderService;
import com.example.demo.service.PricingService;
import com.example.demo.service.impl.OrderServiceImpl;

public class DemoApplication {

    public static void main(String[] args) {
        OrderRepository repository = new InMemoryOrderRepository();
        PricingService pricingService = new PricingService();
        OrderService orderService = new OrderServiceImpl(repository, pricingService);
        OrderController controller = new OrderController(orderService);

        Order first = controller.createOrder(new OrderRequest("Alice", 199.0, "VIP"));
        Order second = controller.createOrder(new OrderRequest("Bob", 50.0, null));

        System.out.println("created=" + first.getId() + " amount=" + first.getAmount());
        System.out.println("created=" + second.getId() + " amount=" + second.getAmount());
        System.out.println("list size=" + controller.listOrders().size());
    }
}
