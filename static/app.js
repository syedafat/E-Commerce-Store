document.addEventListener('DOMContentLoaded',()=>{
 const toggle=document.querySelector('.menu-toggle'), sidebar=document.querySelector('.sidebar');
 if(toggle&&sidebar){toggle.addEventListener('click',()=>{const open=sidebar.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));});document.addEventListener('keydown',e=>{if(e.key==='Escape'){sidebar.classList.remove('open');toggle.setAttribute('aria-expanded','false');}});}
 document.querySelectorAll('[data-confirm]').forEach(form=>form.addEventListener('submit',e=>{if(!window.confirm(form.dataset.confirm))e.preventDefault();}));
 const product=document.querySelector('[name="product_id"]'), qty=document.querySelector('[name="quantity_sold"]'), discount=document.querySelector('[name="discount_applied"]'), preview=document.querySelector('[data-sale-preview]');
 const update=()=>{if(!product||!preview)return;const option=product.selectedOptions[0],price=Number(option?.dataset.price||0),quantity=Number(qty?.value||0),stock=Number(option?.dataset.stock||0),off=Number(discount?.value||0),subtotal=price*quantity;preview.textContent=`${quantity} × Rs. ${price.toFixed(2)} · ${stock} in stock · total Rs. ${Math.max(0,subtotal-off).toFixed(2)}`;};
 [product,qty,discount].forEach(el=>el?.addEventListener('input',update));[product,qty,discount].forEach(el=>el?.addEventListener('change',update));
});
