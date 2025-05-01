import {createContext} from "react";
import all_product from "../Components/Assets/all_product";
import { useState } from "react";


export const ShopContext = createContext(null);
const getDefaultCart = ()=>{
     let cart = {};
     for(let index = 0; index < all_product.length+1; index++){
        cart[index] = 0;
    }
    return cart;
};
 export const ShopContextProvider = (props) => {
       const [cartItems, setCartItems] = useState(getDefaultCart());
        const addToCart = (itemId) => {
            setCartItems((prev) => ({ ...prev, [itemId]: prev[itemId] + 1 }));
            console.log(cartItems);
        };
        const removeFromCart = (itemId) => {
            setCartItems((prev) => ({ ...prev, [itemId]: prev[itemId] - 1 }));
            console.log(cartItems);
        };
        const getTotalCartAmount = () => {
            let totalAmount = 0;
            for (const item in cartItems) {
                if (cartItems[item] > 0) {
                    let itemInfo = all_product.find((product) => product.id === Number(item));
                    console.log("Item Info:", itemInfo);
                    console.log("Quantity:", cartItems[item]);
                    console.log("Price:", itemInfo.new_price);
                    totalAmount += cartItems[item] * itemInfo.new_price;
                }
            }
            console.log("Total Amount:", totalAmount);
            return totalAmount;
        };
        const getItemCountInCart = () => {
            let totalCount = 0;
            for (const item in cartItems) {
                if(cartItems[item] > 0){
                    totalCount += cartItems[item];
                }
            }
            return totalCount;
        };
        const contextValue = { getItemCountInCart,getTotalCartAmount, all_product, cartItems, addToCart, removeFromCart };
    
        return (
                <ShopContext.Provider value={contextValue}>
                    {props.children}
                </ShopContext.Provider>
        );
 };
    
    


export default ShopContextProvider;