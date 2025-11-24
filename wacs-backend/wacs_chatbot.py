from flask import Flask, request, jsonify, send_from_directory, session, send_file
import os
from anthropic import Anthropic  # ✅ Changed from Groq
from flask_cors import CORS
from dotenv import load_dotenv
import chromadb
from sentence_transformers import SentenceTransformer
import numpy as np
from typing import List, Dict
import uuid
import re
import time
from threading import Lock

# Load environment variables
load_dotenv()
anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")  # ✅ Changed
print(f"API Key loaded: {'Yes' if anthropic_api_key else 'No'}")
client = Anthropic(api_key=anthropic_api_key)  # ✅ Changed
app = Flask(__name__)
# 🔑 Secret key for Flask sessions
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "dev-secret"  # fallback for local dev
)
CORS(app)


# In-memory conversation store
conversations = {}
conversations_lock = Lock()

class ConversationManager:
    """Manage conversation state including user names and message history"""
    
    def __init__(self):
        self.conversations = {}
        self.lock = Lock()
        
    def get_or_create_conversation(self, conversation_id: str) -> Dict:
        """Get or create a conversation"""
        with self.lock:
            if conversation_id not in self.conversations:
                self.conversations[conversation_id] = {
                    'user_name': None,
                    'created_at': time.time(),
                    'last_activity': time.time(),
                    'messages': []  # 🆕 Added: Store conversation history
                }
            else:
                self.conversations[conversation_id]['last_activity'] = time.time()
            return self.conversations[conversation_id]
    
    def set_user_name(self, conversation_id: str, name: str):
        """Set user name for a conversation"""
        with self.lock:
            if conversation_id in self.conversations:
                self.conversations[conversation_id]['user_name'] = name
                self.conversations[conversation_id]['last_activity'] = time.time()
    
    def get_user_name(self, conversation_id: str) -> str:
        """Get user name for a conversation"""
        with self.lock:
            conv = self.conversations.get(conversation_id)
            return conv['user_name'] if conv else None
    
    def add_message(self, conversation_id: str, role: str, content: str):
        """🆕 Added: Add a message to conversation history"""
        with self.lock:
            if conversation_id in self.conversations:
                self.conversations[conversation_id]['messages'].append({
                    'role': role,
                    'content': content,
                    'timestamp': time.time()
                })
                # Keep only last 10 messages to avoid token limits
                if len(self.conversations[conversation_id]['messages']) > 10:
                    self.conversations[conversation_id]['messages'] = \
                        self.conversations[conversation_id]['messages'][-10:]
                self.conversations[conversation_id]['last_activity'] = time.time()
    
    def get_conversation_history(self, conversation_id: str, max_messages: int = 10) -> List[Dict]:
        """🆕 Added: Get conversation history"""
        with self.lock:
            conv = self.conversations.get(conversation_id)
            if conv and 'messages' in conv:
                return conv['messages'][-max_messages:]
            return []
    
    def get_full_conversation(self, conversation_id: str) -> Dict:
        """🆕 NEW: Get full conversation data including all messages"""
        with self.lock:
            conv = self.conversations.get(conversation_id)
            if conv:
                return {
                    'user_name': conv.get('user_name'),
                    'messages': conv.get('messages', []),
                    'created_at': conv.get('created_at'),
                    'last_activity': conv.get('last_activity')
                }
            return None
    
    def cleanup_old_conversations(self, max_age_hours: int = 24):
        """Clean up conversations older than max_age_hours"""
        with self.lock:
            current_time = time.time()
            to_remove = []
            for conv_id, conv_data in self.conversations.items():
                if current_time - conv_data['last_activity'] > max_age_hours * 3600:
                    to_remove.append(conv_id)
            
            for conv_id in to_remove:
                del self.conversations[conv_id]

# 🔧 FIX #1: Initialize the ConversationManager
conversation_manager = ConversationManager()

# Initialize SentenceTransformer
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

# WACS Knowledge Base - Updated with WACS FAQ content
wacs_faqs = [
    {
        "question": 'How can I effect stoppage on my loan deductions?',
        "answer": 'For deductions under WACS, the description on your payslip begins with "WACS" followed by the name of the financial institution. Kindly send your letter of non-indebtedness to support@wacs.com.ng. For Cooperative deductions labeled as "COOP" and "CTLS", send your letter to your desk officer. For deductions not on your payslip, contact support@remita.net. For Police, Military, and Paramilitary officers, obtain a letter from the financial institution and forward it to your desk officer.',
        "category": 'loan_deductions'
    },
    {
        "question": 'What is WACS?',
        "answer": 'WACS is an acronym for Workers Aggregated Credit Scheme. It is a platform designed to solve credit access difficulties encountered by civil servants, providing end-to-end solutions for loan management.',
        "category": 'general'
    },
    {
        "question": 'How can I get a Letter of Non-Indebtedness?',
        "answer": ' Kindly contact the microfinance bank you are indebted to for a letter of non indebtedness. For deductions under WACS, the description on your payslip begins with “WACS” followed by the name of the financial institution. Kindly send your letter of non-indebtedness to support@wacs.com.ng.
For Cooperative deductions, these appear on your payslip and are labeled as “COOP.” and “CTLS” Kindly send your letter of non-indebtedness to your desk officer to effect stoppage.    ',
        "category": 'loan_management'
    },
    {
        "question": 'Does IPPIS give out loans?',
        "answer": 'No, IPPIS does not issue loans. However, you can visit the IPPIS-OAGF application to request a loan from any financial institution with the loan product that suits your needs.',
        "category": 'general'
    },
    {
        "question": 'Where can I see my loan deduction?',
        "answer": 'You can view your loan deductions on your payslip, except for loans processed through Remita. For Remita loan details, please contact support@remita.net',
        "category": 'loan_deductions'
    },
    {
        "question": 'How can I get my refund?',
        "answer": 'For refund-related issues, please contact your financial institution directly.',
        "category": 'payment'
    },
    {
        "question": 'Where can I get my payslip?',
        "answer": 'You can obtain your payslip from your desk officer or by logging into the IPPIS-OAGF application using your IPPIS number.',
        "category": 'general'
    },
    {
        "question": 'My net pay is different from what I received as salary. What should I do?',
        "answer": 'Review your payslip to verify all statutory and non-statutory deductions. If the net pay on your payslip differs from what you received, direct your complaint to your financial institution or Remita via support@remita.net',
        "category": 'payment'
    },
    {
        "question": 'How can I get my loan statement?',
        "answer": 'Please contact your financial institution to request your loan statement of account.',
        "category": 'loan_management'
    },
    {
        "question": 'I did not request for a loan, but I was credited by WACS. How do I refund the money?',
        "answer": 'Kindly provide the transaction receipt, name, and IPPIS number to support@wacs.com.ng and a response will be provided within 48 hours.',
        "category": 'loan_management'
    },
    {
        "question": 'I have liquidated my loan, but deductions are still ongoing. What should I do?',
        "answer": 'For WACS deductions (beginning with "WACS" on your payslip), send your letter of non-indebtedness to support@wacs.com.ng. For Cooperative deductions (labeled "COOP" and "CTLS"), send to your desk officer. For deductions not on your payslip, contact support@remita.net. For Police, Military, and Paramilitary officers, obtain a letter from the financial institution and forward to your desk officer.',
        "category": 'loan_deductions'
    },
    {
        "question": 'I was short-paid. What should I do?',
        "answer": 'Kindly review your payslip to see all deductions, as all deductions from your salary are reflected there. You can also call the IPPIS support line at 07002754774 and follow the prompts for assistance.',
        "category": 'payment'
    },
    {
        "question": 'I applied for a loan through the IPPIS-OAGF Mobile application yesterday but I have not received it. What should I do?',
        "answer": 'Loan disbursements are typically processed within 48 hours. If you have not received your loan after this period, kindly send a mail to support@wacs.com.ng with your complaint and feedback will be provided.',
        "category": 'loan_application'
    },
    {
        "question": 'I applied for a loan through a registered lender on the WACS platform but I have not received it. What should I do?',
        "answer": 'Loan disbursements are typically processed within 48 hours. If you have not received your loan after this period, kindly send a mail to support@wacs.com.ng with your complaint and feedback will be provided.',
        "category": 'loan_application'
    },
    {
        "question": 'How can I check my loan balance?',
        "answer": 'Kindly log into the IPPIS-OAGF app to view your loan balance on the dashboard or alternatively contact the lender for the loan balance.',
        "category": 'loan_management'
    },
    {
        "question": 'Who can apply for a loan through IPPIS-OAGF Application?',
        "answer": 'Only Federal Government employees who possess a valid IPPIS number and meet the eligibility criteria are eligible to apply through the platform.',
        "category": 'eligibility'
    },
    {
        "question": 'How much can I borrow through the IPPIS-OAGF Application?',
        "answer": 'The maximum loan amount is determined by the specific loan product and the civil servant\'s eligibility in accordance with civil service rules.',
        "category": 'eligibility'
    },
    {
        "question": 'What is the interest rate for loans offered through IPPIS-OAGF?',
        "answer": 'Interest rates vary based on the loan product offered by each financial institution and are clearly displayed by the respective lenders on the platform.',
        "category": 'loan_terms'
    },
    {
        "question": 'Can I change the repayment schedule for my loan after approval?',
        "answer": 'No. Once a loan is approved, the repayment schedule cannot be modified.',
        "category": 'loan_terms'
    },
    {
        "question": 'Can I apply for a loan through the IPPIS-OAGF Application if I am not a government worker?',
        "answer": 'No. Only Federal Government employees with a valid IPPIS Number are eligible to register on the WACS platform.',
        "category": 'eligibility'
    },
    {
        "question": 'How do I repay my loan?',
        "answer": 'Loan repayments are automatically deducted from your salary.',
        "category": 'loan_repayment'
    },
    {
        "question": 'When do I start repaying my loan?',
        "answer": 'A moratorium period is determined by the financial institution based on the specific loan product you applied for. Please review your loan details carefully.',
        "category": 'loan_repayment'
    },
    {
        "question": 'Which account will my loan be paid into?',
        "answer": 'All loan disbursements are sent directly to your salary account.',
        "category": 'loan_disbursement'
    },
    {
        "question": 'How can I contact IPPIS Support?',
        "answer": 'You can contact IPPIS Support via email at support@ippis.gov.ng or call 0700 275 4774 and follow the prompt.',
        "category": 'support'
    },
    {
        "question": 'How can I differentiate between WACS, Remita, and Cooperative deductions?',
        "answer": 'All WACS deductions begin with the word "WACS" followed by the name of the Financial Institution and appear on your payslip. Cooperative deductions are labeled as "COOP" and also appear on your payslip. However, Remita deductions do not appear on civil servants payslips.',
        "category": 'loan_deductions'
    },
    {
        "question": 'I didn\'t request a loan but was erroneously deducted?',
        "answer": 'Kindly contact IPPIS Support via email at support@ippis.gov.ng or call 0700 275 4774 for assistance.',
        "category": 'loan_deductions'
    },
    {
        "question": 'How can I get Lenders Contact Information?',
        "answer": 'Kindly contact IPPIS Support via email at support@ippis.gov.ng or call 0700 275 4774 for assistance.',
        "category": 'support'
    },
    {
        "question": 'How can I request for a loan?',
        "answer": 'You can request for a loan through the IPPIS-OAGF application portal. Note that only MDA Federal Civil Servants can request for a loan through this application.',
        "category": 'loan_application'
    },
    {
        "question": 'How can I update my phone number?',
        "answer": 'You can update your phone number through your desk officer in your ministry.',
        "category": 'account_management'
    },
    {
        "question": 'I have not received my salary for this Month?',
        "answer": 'Kindly send your Name, IPPIS number, Ministry and bank statement to support@ippis.gov.ng for assistance.',
        "category": 'payment'
    },
    {
        "question": 'I would like to update my maiden name. I just got married.',
        "answer": 'Kindly notify your desk officer to write a letter to the head of service for change of name. Additionally, include a copy of your marriage certificate, newspaper publication and all other necessary documents.',
        "category": 'account_management'
    },
    {
        "question": 'How can I change my Account details on my Payslip?',
        "answer": 'Kindly reach out to your desk officer or payroller to update your account details.',
        "category": 'account_management'
    },
    {
        "question": 'How can I change my date of birth?',
        "answer": 'Kindly submit a written request to the Head of Service through your desk officer to update your date of birth.',
        "category": 'account_management'
    },
    {
        "question": 'I experienced an increase in my loan deduction?',
        "answer": 'Review your payslip to verify all deduction amounts. However, you can direct your complaint to your financial institution.',
        "category": 'loan_deductions'
    }
    
]

class HyperlinkProcessor:
    """Class to handle hyperlink processing for WACS responses"""
    
    @staticmethod
    def convert_to_hyperlinks(text: str) -> str:
        """Convert URLs and email addresses to HTML hyperlinks"""
        # Use placeholders to prevent nested conversions
        placeholders = {}
        placeholder_counter = [0]
        
        def create_placeholder(content):
            placeholder = f"___PLACEHOLDER_{placeholder_counter[0]}___"
            placeholders[placeholder] = content
            placeholder_counter[0] += 1
            return placeholder
        
        # STEP 1: Convert email addresses to mailto links with placeholders
        email_pattern = r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
        
        def email_replacer(match):
            email = match.group(1)
            link = f'<a href="mailto:{email}" style="color: #0066cc; text-decoration: underline; font-weight: 500;">{email}</a>'
            return create_placeholder(link)
        
        result = re.sub(email_pattern, email_replacer, text)
        
        # STEP 2: Convert URLs to hyperlinks (emails are now placeholders, so won't be affected)
        url_pattern = r'((?:https?://)?(?:www\.)?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)'
        
        def url_replacer(match):
            url = match.group(1)
            # Skip if it's a placeholder
            if '___PLACEHOLDER_' in url:
                return url
            
            # Handle specific domain mappings
            href = url
            if not url.startswith('http'):
                if 'www.trade.gov.ng' in url:
                    href = url.replace('www.trade.gov.ng', 'https://trade.gov.ng')
                elif url.startswith('www.'):
                    href = f'https://{url[4:]}'
                else:
                    href = f'https://{url}'
            
            link = f'<a href="{href}" target="_blank" rel="noopener noreferrer" style="color: #0066cc; text-decoration: underline; font-weight: 500;">{url}</a>'
            return create_placeholder(link)
        
        result = re.sub(url_pattern, url_replacer, result)
        
        # STEP 3: Replace placeholders with actual HTML
        for placeholder, content in placeholders.items():
            result = result.replace(placeholder, content)
        
        return result
    
    @staticmethod
    def process_faq_answer(answer: str) -> str:
        """Process FAQ answer to include hyperlinks"""
        return HyperlinkProcessor.convert_to_hyperlinks(answer)

class WACSRAGSystem:
    def __init__(self):
        self.collection_name = "wacs_faqs"
        # Initialize ChromaDB client as instance attribute
        self.chroma_client = chromadb.Client()
        self.hyperlink_processor = HyperlinkProcessor()
        self.setup_vector_database()
    
    def setup_vector_database(self):
        """Initialize ChromaDB collection with WACS FAQs"""
        try:
            # Delete existing collection if it exists
            try:
                self.chroma_client.delete_collection(name=self.collection_name)
            except:
                pass
            
            # Create new collection
            self.collection = self.chroma_client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
            
            # Prepare documents for embedding
            documents = []
            metadatas = []
            ids = []
            
            for i, faq in enumerate(wacs_faqs):
                # Combine question and answer for better context
                doc_text = f"Question: {faq['question']}\nAnswer: {faq['answer']}"
                documents.append(doc_text)
                metadatas.append({
                    "category": faq['category'],
                    "question": faq['question'],
                    "answer": faq['answer']
                })
                ids.append(str(uuid.uuid4()))
            
            # Add documents to collection
            self.collection.add(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            
            print(f"✅ Vector database initialized with {len(wacs_faqs)} FAQs")
            
        except Exception as e:
            print(f"❌ Error setting up vector database: {e}")
    
    def retrieve_relevant_faqs(self, query: str, n_results: int = 3) -> List[Dict]:
        """Retrieve most relevant FAQs based on user query"""
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results
            )
            
            relevant_faqs = []
            if results['metadatas'] and len(results['metadatas'][0]) > 0:
                for metadata in results['metadatas'][0]:
                    relevant_faqs.append({
                        "question": metadata['question'],
                        "answer": metadata['answer'],
                        "category": metadata['category']
                    })
            
            return relevant_faqs
            
        except Exception as e:
            print(f"❌ Error retrieving FAQs: {e}")
            return []
    
    def generate_rag_response(self, user_query: str, user_name: str = None, conversation_history: List[Dict] = None) -> Dict:
        """Generate response using RAG with conversation context"""
        try:
            # Step 1: Retrieve relevant FAQs
            relevant_faqs = self.retrieve_relevant_faqs(user_query, n_results=3)
            
            # Step 2: Build context from relevant FAQs
            context = ""
            if relevant_faqs:
                context = "Here are relevant FAQs that might help answer the question:\n\n"
                for i, faq in enumerate(relevant_faqs, 1):
                    context += f"FAQ {i}:\nQ: {faq['question']}\nA: {faq['answer']}\n\n"
            
            # Step 3: Create system prompt with user context
            user_context = f"The user's name is {user_name}." if user_name else ""
            
            # ✅ UPDATED: Friendly but concise system prompt for WACS
            system_prompt = f"""You are a friendly WACS (Workers Aggregated Credit Scheme) support assistant helping Federal Government civil servants with loan management and IPPIS-related queries. {user_context}

TONE & STYLE - THIS IS CRITICAL:
- Be warm, helpful, and show you care about their issue
- Keep responses SHORT - aim for 2-4 sentences maximum
- Use natural, conversational language like you're texting a friend
- Show empathy when they're frustrated ("I know this is frustrating, let's fix it!")
- End with a friendly offer to help more

AVOID THESE:
- Long explanations - get to the point quickly
- Robotic phrases like "I have processed..." or "Please be advised..."
- Repeating yourself or over-explaining
- Multiple paragraphs when 1-2 sentences work
- Using their name repeatedly (sounds fake)

GOOD EXAMPLES:
✅ "I see the issue! Check your payslip for WACS deductions - they start with 'WACS' followed by the lender name. Send your non-indebtedness letter to support@wacs.com.ng to stop it."
✅ "Loans typically arrive within 48 hours. Still waiting? Drop a mail to support@wacs.com.ng with your details and they'll sort it out!"
✅ "Got it! Log into the IPPIS-OAGF app to see your loan balance on the dashboard. Easy!"

BAD EXAMPLES (too long/robotic):
❌ "I understand you are experiencing difficulties with your loan deduction. This is a common issue that many users face. Let me provide you with some steps..."
❌ "Thank you for reaching out. I would be happy to assist you with this matter. Based on the information provided in our system..."

KEY RULES:
1. Jump straight to the solution - no long intros
2. Use the FAQ context provided but rewrite in your own friendly words
3. If you don't know, guide them to support@wacs.com.ng (WACS issues), support@ippis.gov.ng (IPPIS issues), or support@remita.net (Remita issues)
4. Always use exact format for contacts: support@wacs.com.ng, support@ippis.gov.ng, support@remita.net
5. Pay attention to conversation history - if they already tried your advice, offer alternatives instead of repeating
6. For "thank you" messages: keep it super brief - just "You're welcome! Happy to help 😊" or similar
7. Use names ONLY in initial greeting, then avoid unless adding personal touch after long conversation
8. When mentioning emails, use natural phrasing, never mention "FAQs" or "knowledge base"

CONTACT INFO (use when relevant):
- WACS Support: support@wacs.com.ng
- IPPIS Support: support@ippis.gov.ng, Phone: 0700 275 4774
- Remita Support: support@remita.net
- TIN validation: www.trade.gov.ng (Agencies > FIRS)

LOAN DEDUCTION TYPES:
- WACS deductions: Start with "WACS" on payslip, contact support@wacs.com.ng
- Cooperative deductions: Labeled "COOP" and "CTLS" on payslip, contact desk officer
- Remita deductions: Don't appear on civil servants' payslips, contact support@remita.net"""
            
            # Step 4: Build conversation messages with history
            # ✅ UPDATED: Changed to Anthropic format
            messages = []
            
            # Add conversation history if available (last 6 messages)
            if conversation_history:
                for msg in conversation_history[-6:]:
                    messages.append({
                        "role": "user" if msg['role'] == "user" else "assistant",
                        "content": msg['content']
                    })
            
            # Step 5: Add current user query with context
            if context:
                current_prompt = f"{context}\n\nUser Question: {user_query}\n\nProvide a friendly, concise response based on the FAQ context and conversation history. Remember: be warm but brief!"
            else:
                current_prompt = f"User Question: {user_query}\n\nProvide a friendly, concise response about WACS and IPPIS processes."
            
            messages.append({"role": "user", "content": current_prompt})
            
            # Step 6: Generate response using Claude
            # ✅ UPDATED: Changed to Anthropic API format
            response = client.messages.create(
                model="claude-sonnet-4-5-20250929",  # ✅ Using Claude Sonnet 4.5
                max_tokens=450,  # ✅ Limit for concise responses
                temperature=0.7,  # ✅ Natural, conversational tone
                system=system_prompt,  # ✅ System prompt separate in Anthropic
                messages=messages
            )
            
            raw_response = response.content[0].text  # ✅ Extract text from Claude response
            
            # Step 7: Process response to add hyperlinks
            processed_response = self.hyperlink_processor.convert_to_hyperlinks(raw_response)
            
            # Step 8: Return both versions
            return {
                "response": raw_response,
                "response_with_links": processed_response,
                "relevant_faqs": relevant_faqs,
                "context_used": bool(context)
            }
            
        except Exception as e:
            print(f"❌ Error generating RAG response: {e}")
            error_message = "Oops! I'm having a moment here. Can you try again, or reach out to support@wacs.com.ng?"
            return {
                "response": error_message,
                "response_with_links": self.hyperlink_processor.convert_to_hyperlinks(error_message),
                "relevant_faqs": [],
                "context_used": False
            }

# Initialize RAG system
rag_system = WACSRAGSystem()

# Initialize conversation manager
conversation_manager = ConversationManager()

def extract_name_from_message(message: str) -> str:
    """Extract name from user message"""
    message_lower = message.lower().strip()
    
    # Common patterns for name introduction - ONLY explicit name patterns
    name_patterns = [
        r"my name is\s+(\w+)",
        r"i'm\s+(\w+)",
        r"i am\s+(\w+)",
        r"call me\s+(\w+)",
        r"it's\s+(\w+)",
        r"this is\s+(\w+)",
        r"name:\s*(\w+)",
        r"^([a-zA-Z]{2,})$"  # Single word with at least 2 letters (any case)
    ]
    
    # Expanded list of common non-names to avoid
    non_names = [
        'hi', 'hello', 'hey', 'good', 'morning', 'afternoon', 'evening',
        'yes', 'no', 'ok', 'okay', 'sure', 'please', 'help', 'thanks', 'thank',
        'what', 'how', 'when', 'where', 'why', 'who', 'which',
        'wacs', 'ippis', 'loan', 'deduction', 'payment', 'salary', 'support',
        'certificate', 'problem', 'issue', 'error', 'refund', 'balance',
        'can', 'will', 'should', 'could', 'would', 'need', 'want', 'like',
        'get', 'have', 'make', 'take', 'give', 'find', 'know', 'think',
        'see', 'look', 'check', 'try', 'use', 'work', 'go', 'come'
    ]
    
    for pattern in name_patterns:
        match = re.search(pattern, message_lower)
        if match:
            potential_name = match.group(1).strip()
            
            # For single word pattern, be more strict
            if pattern == r"^(\w+)$":
                # Must be at least 2 characters, start with capital when original, and not in non-names
                original_word = message.strip()
                if (len(potential_name) >= 2 and 
                    potential_name.lower() not in non_names and 
                    original_word[0].isupper() and  # Original message starts with capital
                    original_word.isalpha()):  # Contains only letters
                    return potential_name.capitalize()
            else:
                # For explicit patterns like "my name is", be less strict
                if (len(potential_name) >= 2 and 
                    potential_name.lower() not in non_names):
                    return potential_name.capitalize()
    
    return None

@app.route("/chat", methods=["POST"])
def chat():
    user_input = request.json.get("message")
    conversation_id = request.json.get("conversation_id")
    
    # 🔧 FIX #2: Generate unique conversation_id if not provided
    if not conversation_id or conversation_id == "default":
        conversation_id = str(uuid.uuid4())
        print(f"🆕 Generated new conversation_id: {conversation_id}")
    
    if not user_input:
        return jsonify({"error": "No message received"}), 400
    
    try:
        # Get or create conversation
        conversation = conversation_manager.get_or_create_conversation(conversation_id)
        user_name = conversation.get('user_name')
        
        # If no name in conversation, first check if this is a name response
        if not user_name:
            extracted_name = extract_name_from_message(user_input)
            if extracted_name:
                conversation_manager.set_user_name(conversation_id, extracted_name)
                user_name = extracted_name
                # Acknowledge the name and ask how to help
                response = f"Hello {user_name}! Nice to meet you 😊 How can I help you today?"
                processed_response = rag_system.hyperlink_processor.convert_to_hyperlinks(response)
                
                # 🆕 Store the bot's greeting in history
                conversation_manager.add_message(conversation_id, "assistant", response)
                
                return jsonify({
                    "reply": processed_response,
                    "raw_reply": response,
                    "relevant_faqs": [],
                    "context_used": False,
                    "name_captured": True,
                    "conversation_id": conversation_id
                })
            else:
                # Ask for name if not provided and not in conversation
                # Don't treat greetings as requests for help
                greeting_words = ['hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening']
                if any(greeting in user_input.lower() for greeting in greeting_words):
                    response = "Hello! May I know your name?"
                    conversation_manager.add_message(conversation_id, "assistant", response)
                    return jsonify({
                        "reply": response,
                        "raw_reply": response,
                        "relevant_faqs": [],
                        "context_used": False,
                        "asking_for_name": True,
                        "conversation_id": conversation_id
                    })
                else:
                    response = "May I know your name?"
                    conversation_manager.add_message(conversation_id, "assistant", response)
                    return jsonify({
                        "reply": response,
                        "raw_reply": response,
                        "relevant_faqs": [],
                        "context_used": False,
                        "asking_for_name": True,
                        "conversation_id": conversation_id
                    })
        
        # 🆕 Store user message in history
        conversation_manager.add_message(conversation_id, "user", user_input)
        
        # 🆕 Get conversation history
        conversation_history = conversation_manager.get_conversation_history(conversation_id)
        
        # Generate response using RAG with user name and conversation history
        response_data = rag_system.generate_rag_response(
            user_input, 
            user_name,
            conversation_history  # 🆕 Pass conversation history
        )
        
        # 🆕 Store bot response in history
        conversation_manager.add_message(conversation_id, "assistant", response_data["response"])
        
        return jsonify({
            "reply": response_data["response_with_links"],  # Send processed response with links
            "raw_reply": response_data["response"],  # Also include raw response
            "relevant_faqs": response_data["relevant_faqs"],
            "context_used": response_data["context_used"],
            "user_name": user_name,
            "conversation_id": conversation_id
        })
    
    except Exception as e:
        print(f"❌ Error in chat endpoint: {e}")
        return jsonify({"error": "Internal server error"}), 500

# 🆕 NEW ENDPOINT: Get conversation history for persistence
@app.route("/get-conversation", methods=["POST"])
def get_conversation():
    """Get full conversation history for a given conversation_id"""
    conversation_id = request.json.get("conversation_id")
    
    if not conversation_id:
        return jsonify({"error": "No conversation_id provided"}), 400
    
    try:
        conversation_data = conversation_manager.get_full_conversation(conversation_id)
        
        if conversation_data:
            # Process messages to add hyperlinks
            processed_messages = []
            for msg in conversation_data.get('messages', []):
                processed_content = rag_system.hyperlink_processor.convert_to_hyperlinks(msg['content'])
                processed_messages.append({
                    'role': msg['role'],
                    'content': processed_content,
                    'raw_content': msg['content'],
                    'timestamp': msg.get('timestamp')
                })
            
            return jsonify({
                "success": True,
                "conversation_id": conversation_id,
                "user_name": conversation_data.get('user_name'),
                "messages": processed_messages,
                "created_at": conversation_data.get('created_at'),
                "last_activity": conversation_data.get('last_activity')
            })
        else:
            return jsonify({
                "success": False,
                "message": "Conversation not found"
            }), 404
    
    except Exception as e:
        print(f"❌ Error in get-conversation endpoint: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/reset-session", methods=["POST"])
def reset_session():
    """Reset user session (clear name)"""
    session.clear()
    return jsonify({"message": "Session reset successfully"})

@app.route("/get-session", methods=["GET"])
def get_session():
    """Get current session info"""
    return jsonify({
        "user_name": session.get('user_name'),
        "has_name": bool(session.get('user_name'))
    })

@app.route("/search", methods=["POST"])
def search_faqs():
    """Endpoint to search FAQs directly"""
    query = request.json.get("query")
    if not query:
        return jsonify({"error": "No query provided"}), 400
    
    try:
        relevant_faqs = rag_system.retrieve_relevant_faqs(query, n_results=5)
        return jsonify({"faqs": relevant_faqs})
    
    except Exception as e:
        print(f"❌ Error in search endpoint: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "rag_system": "operational",
        "model": "claude-sonnet-4-5",
        "total_faqs": len(wacs_faqs),
        "hyperlink_processing": "enabled",
        "session_support": "enabled",
        "conversation_memory": "enabled",
        "conversation_persistence": "enabled"
    })

@app.route("/process-text", methods=["POST"])
def process_text():
    """Endpoint to process any text and add hyperlinks"""
    text = request.json.get("text")
    if not text:
        return jsonify({"error": "No text provided"}), 400
    
    try:
        processed_text = rag_system.hyperlink_processor.convert_to_hyperlinks(text)
        return jsonify({
            "original_text": text,
            "processed_text": processed_text
        })
    
    except Exception as e:
        print(f"❌ Error in process-text endpoint: {e}")
        return jsonify({"error": "Internal server error"}), 500


# ==========================================================
# ✅ Frontend and Static Serving
# ==========================================================

@app.route('/')
def serve_frontend():
    """Serve the main frontend page"""
    try:
        return send_file('frontend/index2.html')
    except Exception as e:
        print(f"Error serving frontend: {e}")
        return f"Frontend error: {e}", 500

@app.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static files"""
    try:
        return send_from_directory('frontend', filename)
    except Exception as e:
        return f"Static file error: {e}", 404

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    print(f"🚀 Starting WACS Chatbot with Claude Sonnet 4.5 on port {port}")
    print(f"📁 Working directory: {os.getcwd()}")
    print(f"📄 Frontend exists: {os.path.exists('frontend/index2.html')}")
    
    app.run(
        host='0.0.0.0',  # MUST be 0.0.0.0 for Cloud Run
        port=port,
        debug=False,  # Disable debug in production
        threaded=True
    )
